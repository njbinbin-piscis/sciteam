"""LLM-backed worker implementing RuntimePort.

Each subagent call becomes one (or two, on JSON repair) chat completions:
constitution + role prompt + skill pack + mission brief + compacted team
context. Workers answer in a structured JSON envelope; when the envelope
carries an `artifact` object, it is written to the mission artifact path
(the exit contract is validated by the engine, not by the worker).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sciteam.limits as limits
from sciteam import worker_common
from sciteam.compact import build_working_pack
from sciteam.experiment_pack import PackError, ensure_skills_in_pack
from sciteam.llm import LlmClient
from sciteam.memory import CampaignMemoryStore, MemoryRecord, SeatMemoryStore
from sciteam.output_truncation import persist_full_text
from sciteam.persist import (
    append_agent_log,
    write_agent_result_file,
)
from sciteam.runtime import AgentOutcome, AgentRunResult, Run, RunSpec, RunState
from sciteam.worker_common import extract_json as _extract_json
from sciteam.worker_common import role_key as _role_key
from sciteam.worker_common import sanitize_artifact_payload

# Inner-loop tool rounds per work item (LLM↔tools until final envelope).
# Override with SCITEAM_MAX_TOOL_ROUNDS. Five was a scaffold cap and is far too low
# for literature / research work items. The default supports research-grade iteration.
_DEFAULT_MAX_TOOL_ROUNDS = 500

# P0-3: readonly tool calls in the same round (e.g. several literature_search
# calls) run concurrently via this many worker threads; side_effect calls
# always run serially and in order. Override with SCITEAM_MAX_PARALLEL_READONLY.
_DEFAULT_MAX_PARALLEL_READONLY = 4

# P1-7: max schema-validator-guided repair round-trips for a contract
# ("artifact") violation before giving up and letting the engine's own
# downstream ContractValidator reject the artifact as before.
_MAX_CONTRACT_REPAIR_ATTEMPTS = 2
_JSON_OBJECT_RESPONSE_FORMAT = {"type": "json_object"}

# P1-6: once accumulated `role: "tool"` message content in a single work
# item's inner loop exceeds this many chars, older tool results are folded
# into a one-line digest and their full JSON is persisted to
# `<mission_workspace>/tool_history/*.json`. The most recent
# `_TOOL_HISTORY_KEEP_RECENT` tool messages are always kept verbatim so the
# model can still reason about what it just did. This is a *within-turn*
# analogue of the working-pack compaction in `compact.py`, for the raw
# tool-calling transcript that `_fit_to_context` does not see (it only sizes
# the initial prompt, not turns added mid-loop).
#
# Honesty note (HARNESS_SOTA_AUDIT.md P1-6): the disk pointer is currently
# for audit/replay (Observatory, `tool_history/*.json` on the mission
# workspace) — there is no tool-calling capability that lets the *same*
# agent re-open a compacted pointer mid-session yet. Wiring that up needs a
# capability-gated read tool and is tracked as a fast-follow, not silently
# implied here.
_DEFAULT_TOOL_HISTORY_BUDGET_CHARS = 20_000
_TOOL_HISTORY_KEEP_RECENT = 3

# P1-8: full-allowlist skill injection (every id, each capped at
# `max_skill_chars`) doesn't scale — once a role's allowlist grows past this
# many skills, only the top-`_SKILL_TOPK_K` by deterministic lexical overlap
# with the task card get their body text injected into the prompt. This is
# strictly a *prompt-injection* selection: `_required_tool_capabilities`
# still derives from the FULL allowlist (see `run_subagent`), so which
# capabilities/tools a seat is granted never shrinks because a skill's text
# wasn't shown this round (capability surface unchanged, per audit).
_SKILL_TOPK_THRESHOLD = 6
_SKILL_TOPK_K = 6

ENVELOPE_INSTRUCTIONS = """\
You may call research tools (literature_search, web_search, url_fetch, citation_audit)
before finishing. When the mission needs literature or external facts, you MUST
use tools — do not invent DOIs, titles, or metrics.

You also have workspace-confined file tools, always available: read, write, edit,
ls, find, grep. Use `read`/`grep`/`find`/`ls` to inspect existing files (a problem
spec, another seat's output, prior code) before acting — never assume or invent
their contents. Use `write`/`edit` for incremental work you want on disk *now*
(these take effect immediately, unlike the round-end "files" key below).
For source code or any long content, you MUST use `write`/`edit`. Do not place
source code in the JSON `files` value; after writing it with a tool, emit
`"files": null`.

When finished, respond with a single JSON object (no markdown fences, no commentary outside JSON):
{
  "summary": "<what you concluded / produced, concise but complete>",
  "facts": ["<atomic reusable finding>", ...],
  "artifact": <the exit-contract JSON object if YOU are finalizing it this round, else null>,
  "files": {"<relative filename>": "<full file content>", ...} | null,
  "run_eval": {"problem_id": "<registered id>", "candidate_file": "<file you wrote>"} | null,
  "tool_notes": ["<optional: which tools you used and why>"] | null
}
Only emit "artifact" when your role is responsible for assembling the final
mission artifact and the content is ready; partial work belongs in summary/facts.
Inside the artifact, OMIT optional fields you cannot fill — never emit null
for them, and never add fields the schema does not declare.
"files" is only for a short text note that cannot be written through the
workspace tools. Candidate code and manuscripts must be written with
`write`/`edit`. "run_eval" invokes the FROZEN eval script on a candidate file
you wrote; its results.json is written to the mission artifact path
automatically — this is the ONLY way official numbers come to exist. Use these
only when the mission calls for them; otherwise emit null.
"""

ASSESS_ENVELOPE_INSTRUCTIONS = """\
You are a round-assessment seat (may_assess_round). You do NOT write the mission
exit-contract file. Apply the charter to the observation pack and decide.

When finished, respond with a single JSON object (no markdown fences):
{
  "decision": "continue" | "completed" | "failed",
  "reason_code": "ok_complete" | "need_artifact" | "gate_unsatisfied" | "exit_stalled" |
    "integrity_fail" | "process_fail" | "all_workers_failed",
  "summary": "<short rationale>",
  "next_round_hint": "<guidance for production seats, or empty string>",
  "assessor_key": "<your seat key>",
  "approve_promote_draft": false,
  "facts": []
}
"artifact" must be null. Do not invent science content.
"""

# B4: appended to whichever envelope-instructions block is active, only for
# seats whose declared capabilities already justify process-level access
# (`worker_common.BASH_GATING_CAPABILITIES`) — mirrors the pi arm's
# capability-gated `bash` (see `pi_runtime._PI_ARTIFACT_INSTRUCTIONS`'s
# `bash_clause`), so a skill allowlist grants the same shell access on
# either arm.
_BASH_TOOL_CLAUSE = """
You also have `bash`: a shell command cwd-confined to the mission workspace,
bounded in time/CPU/memory with truncated output. Use it for build/test/compute
steps your other tools cannot do (running a build, invoking a CLI, inspecting an
environment) — never to fetch URLs when literature_search/web_search/url_fetch
already cover that, and never to work around the workspace confinement.
"""


def _assessment_envelope_ok(envelope: dict[str, Any] | None) -> bool:
    if not isinstance(envelope, dict):
        return False
    decision = str(envelope.get("decision") or "").strip().lower()
    reason = str(envelope.get("reason_code") or "").strip()
    return decision in {"continue", "completed", "failed"} and bool(reason)


def _render_inputs_budgeted(data: dict[str, Any], budget: int) -> str:
    """Render a mapping as sectioned JSON under a total character budget.

    Naive tail-truncation of one big JSON dump silently drops whole entries;
    instead we flatten one nesting level and give every section a fair share
    (smallest first, leftovers redistributed), so small load-bearing sections
    always survive intact and only oversized ones get truncated."""
    entries: list[tuple[str, str]] = []
    for key, value in data.items():
        if (
            isinstance(value, dict)
            and value
            and all(isinstance(v, (dict, list)) for v in value.values())
        ):
            for sub_key, sub_value in value.items():
                entries.append(
                    (f"{key}/{sub_key}", json.dumps(sub_value, ensure_ascii=False, indent=2))
                )
        else:
            entries.append((key, json.dumps(value, ensure_ascii=False, indent=2)))
    rendered: dict[str, str] = {}
    remaining = max(0, budget)
    for key, text in sorted(entries, key=lambda item: len(item[1])):
        pending = len(entries) - len(rendered)
        share = remaining // max(1, pending)
        if len(text) > share:
            text = text[: max(0, share - 20)] + "\n...[truncated]"
        rendered[key] = text
        remaining -= len(text)
    return "\n\n".join(f"## {key}\n{rendered[key]}" for key, _ in entries)


@dataclass
class PromptAssets:
    """Filesystem-backed prompt/skill lookup.

    When ``pack_dir`` is set (experiment instance root), role prompts and skills
    are loaded from the pack first; system ``assets/`` remain templates.

    ``genome_overlay_dir`` (Line D, lineD/03 §11): when set, every
    ``prompt_modules/*.md`` under it is appended to each role prompt as an
    evolved-guidance section. ``None`` (the default) is byte-identical to the
    pre-genome behavior — the Line A arms never pass it.

    ``extra_prompts_dirs``/``extra_skills_dirs`` (see
    ``docs/25-sciteam-plugin-architecture.md`` §3.3,
    ``PluginRegistry.role_prompts_dirs``/``skills_dirs``): additional search
    directories consulted, in order, after ``prompts_dir``/``skills_dir`` and
    ``pack_dir`` find no match. Empty tuples (the default) reproduce the
    pre-plugin lookup order byte-for-byte — a plugin can *add* a role prompt
    or skill body for an id the base assets don't define, never shadow one
    that already resolves from ``prompts_dir``/``skills_dir``/``pack_dir``.
    """

    prompts_dir: Path
    skills_dir: Path
    max_skill_chars: int = 16000
    pack_dir: Path | None = None
    genome_overlay_dir: Path | None = None
    extra_prompts_dirs: tuple[Path, ...] = ()
    extra_skills_dirs: tuple[Path, ...] = ()
    loaded_skill_versions: dict[str, dict[str, str]] = field(default_factory=dict, init=False)

    def constitution(self) -> str:
        path = self.prompts_dir / "constitution.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def role_prompt(self, agent_key: str) -> str:
        return self._base_role_prompt(agent_key) + self._genome_modules()

    def _base_role_prompt(self, agent_key: str) -> str:
        role = _role_key(agent_key)
        if self.pack_dir is not None:
            pack_agent = Path(self.pack_dir) / "agents" / role / "AGENT.md"
            if pack_agent.is_file():
                text = pack_agent.read_text(encoding="utf-8")
                # Strip YAML frontmatter for the model-facing prompt body.
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        text = parts[2].lstrip("\n")
                return text
        search_dirs = (self.prompts_dir, *self.extra_prompts_dirs)
        # Pass 1: a role-specific file, in any search dir — a plugin's
        # specific role must be found before falling through to the base's
        # generic `worker.md` catch-all (pass 2), or a plugin-added role
        # would never be reachable (the base ships a `worker.md`, so it
        # would always win pass 1 of a single combined per-dir loop).
        for prompts_dir in search_dirs:
            path = Path(prompts_dir) / "roles" / f"{role}.md"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        # Pass 2: the legacy generic fallback, same search order.
        for prompts_dir in search_dirs:
            path = Path(prompts_dir) / "roles" / "worker.md"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return f"You are the `{role}` specialist on a research team."

    def _genome_modules(self) -> str:
        if self.genome_overlay_dir is None:
            return ""
        mod_dir = Path(self.genome_overlay_dir) / "prompt_modules"
        if not mod_dir.is_dir():
            return ""
        parts = [
            body
            for path in sorted(mod_dir.glob("*.md"))
            if (body := path.read_text(encoding="utf-8").strip())
        ]
        if not parts:
            return ""
        return "\n\n## Evolved guidance (genome overlay)\n\n" + "\n\n".join(parts)

    def skill_raw_text(self, skill_id: str) -> tuple[str, str]:
        """`(text, source_path)` for one skill id, pack override first, then
        the system `skills_dir` — the same lookup `skill_pack` uses per
        entry, factored out so P1-8 top-k scoring can read skill bodies
        without duplicating (and risking drifting from) this lookup order."""
        if self.pack_dir is not None:
            pack_skill = Path(self.pack_dir) / "skills" / skill_id / "SKILL.md"
            if pack_skill.is_file():
                return pack_skill.read_text(encoding="utf-8"), str(pack_skill)
        for skills_dir in (self.skills_dir, *self.extra_skills_dirs):
            path = Path(skills_dir) / skill_id / "SKILL.md"
            if path.is_file():
                return path.read_text(encoding="utf-8"), str(path)
        return "", ""

    def skill_pack(self, allowlist: list[str], *, max_chars_override: int | None = None) -> str:
        max_chars = self.max_skill_chars if max_chars_override is None else max_chars_override
        sections: list[str] = []
        for skill_id in allowlist:
            text, source = self.skill_raw_text(skill_id)
            if text:
                version_match = re.search(r"(?m)^(?:version:\s*|##\s+Version\s*\n)([^\n]+)", text)
                self.loaded_skill_versions[skill_id] = {
                    "version": (
                        version_match.group(1).strip().strip("\"'")
                        if version_match
                        else "unversioned"
                    ),
                    "source": source,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n...[truncated]"
                sections.append(f'<skill id="{skill_id}">\n{text}\n</skill>')
            else:
                sections.append(
                    f'<skill id="{skill_id}">(spec not found — follow the id '
                    "semantics conservatively)</skill>"
                )
        return "\n\n".join(sections)


class LlmWorkerRuntime:
    """RuntimePort implementation running each task as an LLM call."""

    def __init__(
        self,
        *,
        client: LlmClient,
        assets: PromptAssets,
        schemas_dir: Path | str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        max_tool_rounds: int | None = None,
        eval_runner: Any = None,
    ) -> None:
        """`eval_runner` is an injected verifier callable
        (problem_id, candidate_path | None, out_path, seed | None) -> None;
        the engine knows nothing about what it verifies.

        Exit-artifact duty is NOT a hardcoded role list here. MissionRunner
        injects ``metadata.artifact_emit_roles`` from the paradigm YAML
        (``emits_exit_artifact: true`` on member seats).
        """
        self._client = client
        self._assets = assets
        self._schemas_dir = Path(schemas_dir)
        self._temperature = temperature
        if max_tokens is None:
            max_tokens = int(
                getattr(getattr(client, "config", None), "default_max_tokens", limits.MAX_TOKENS)
            )
        self._max_tokens = max_tokens
        self._tool_result_visible_chars = limits.tool_result_visible_chars()
        self._tool_result_preview_chars = limits.tool_result_preview_chars()
        if max_tool_rounds is None:
            try:
                max_tool_rounds = int(
                    os.environ.get("SCITEAM_MAX_TOOL_ROUNDS") or _DEFAULT_MAX_TOOL_ROUNDS
                )
            except ValueError:
                max_tool_rounds = _DEFAULT_MAX_TOOL_ROUNDS
        self._max_tool_rounds = max(1, int(max_tool_rounds))
        try:
            self._max_parallel_readonly = max(
                1,
                int(
                    os.environ.get("SCITEAM_MAX_PARALLEL_READONLY")
                    or _DEFAULT_MAX_PARALLEL_READONLY
                ),
            )
        except ValueError:
            self._max_parallel_readonly = _DEFAULT_MAX_PARALLEL_READONLY
        try:
            self._tool_history_budget_chars = max(
                4_000,
                int(
                    os.environ.get("SCITEAM_TOOL_HISTORY_BUDGET_CHARS")
                    or _DEFAULT_TOOL_HISTORY_BUDGET_CHARS
                ),
            )
        except ValueError:
            self._tool_history_budget_chars = _DEFAULT_TOOL_HISTORY_BUDGET_CHARS
        self._eval_runner = eval_runner
        self._task_history: dict[str, list[dict[str, Any]]] = {}
        self.transcript: list[dict[str, Any]] = []

    def _commit_memory_record(
        self,
        *,
        work_dir: str,
        agent_key: str,
        meta: dict[str, Any],
        record: MemoryRecord,
    ) -> None:
        """Commit to the seat's own history, and — when the mission was
        created with a `campaign_memory_path` pointer — to this role's
        cross-mission memory too (P1-5). Both stores self-compact when they
        grow past their bound, so committing on every outcome (not just
        failure/repair) does not grow the file unbounded.
        """
        worker_common.commit_memory_record(
            work_dir=work_dir, agent_key=agent_key, meta=meta, record=record
        )

    def _commit_failure_memory(
        self,
        *,
        team_run_id: str,
        agent_key: str,
        work_dir: str,
        context: dict[str, Any],
        task: str,
        reason_code: str,
        diagnosis: str,
        counterexample: str,
    ) -> None:
        row = {
            "agent_key": agent_key,
            "round_index": context.get("round_index", 0),
            "work_item_id": str(context.get("work_item_id") or ""),
            "state": "failed",
            "result": "",
            "error": diagnosis,
            "reason_code": reason_code,
        }
        self._task_history.setdefault(team_run_id, []).append(row)
        with contextlib.suppress(OSError):
            self._commit_memory_record(
                work_dir=work_dir,
                agent_key=agent_key,
                meta=dict(context.get("team_metadata") or {}),
                record=MemoryRecord(
                    outcome="failure",
                    work_item_id=row["work_item_id"],
                    round_index=int(row["round_index"] or 0),
                    counterexample=counterexample[:4000],
                    attempted_method=task[:2000],
                    evidence_refs=(
                        f"work_item:{row['work_item_id']}",
                        f"round:{row['round_index']}",
                    ),
                    reason_code=reason_code,
                ),
            )

    @property
    def usage(self):
        return self._client.usage

    def _contract_schema_text(self, contract: str) -> str:
        return worker_common.contract_schema_text(self._schemas_dir, contract)

    @staticmethod
    def _artifact_duty_tokens(meta: dict[str, Any] | None) -> set[str]:
        return worker_common.artifact_duty_tokens(meta)

    def _is_artifact_role(self, agent_key: str, meta: dict[str, Any] | None = None) -> bool:
        """True only if paradigm metadata lists this seat as exit-artifact duty."""
        return worker_common.is_artifact_role(agent_key, meta)

    @staticmethod
    def _assess_role_tokens(meta: dict[str, Any] | None) -> set[str]:
        return worker_common.assess_role_tokens(meta)

    def _is_assess_role(self, agent_key: str, meta: dict[str, Any] | None = None) -> bool:
        return worker_common.is_assess_role(agent_key, meta)

    def _research_tools_enabled(self, skills_allowlist: list[str], mission_id: str) -> bool:
        """Enable research tools when allowlisted skills declare research caps.

        Generic: driven by skill ``Requires capabilities``, not by mission-id
        heuristics or a hard-coded lit-only skill set.
        """
        from sciteam.capabilities import research_tools_needed

        return research_tools_needed(self._required_tool_capabilities(skills_allowlist, mission_id))

    def _required_tool_capabilities(self, skills_allowlist: list[str], mission_id: str) -> set[str]:
        del mission_id  # reserved for audit/logging; not a capability heuristic
        return worker_common.required_tool_capabilities(self._assets.skills_dir, skills_allowlist)

    def _select_topk_skills(
        self, task: str, allowlist: list[str]
    ) -> tuple[list[str], bool, list[tuple[str, int]]]:
        """P1-8: `(injected_ids, applied, scored)`.

        Below `_SKILL_TOPK_THRESHOLD` the full allowlist is injected
        unchanged (`applied=False`) — this only kicks in for roles with
        unusually large allowlists. Selection is deterministic word-overlap
        between the task card and each skill's id + body text (same
        family of scorer as P1-5 memory recall, not embeddings, so results
        are byte-reproducible given the same allowlist/task/skill files),
        ties broken by the allowlist's own declared order so the choice
        never depends on dict/set iteration order.
        """
        if len(allowlist) <= _SKILL_TOPK_THRESHOLD:
            return list(allowlist), False, []
        from sciteam.memory import tokenize

        query_tokens = tokenize(task)
        scored: list[tuple[int, int, str]] = []
        for idx, skill_id in enumerate(allowlist):
            text, _source = self._assets.skill_raw_text(skill_id)
            haystack_tokens = tokenize(f"{skill_id} {text}")
            score = len(query_tokens & haystack_tokens) if query_tokens else 0
            scored.append((score, idx, skill_id))
        ranked = sorted(scored, key=lambda t: (-t[0], t[1]))
        top = ranked[:_SKILL_TOPK_K]
        selected_idx = {idx for _, idx, _ in top}
        # Preserve the allowlist's original relative order for the survivors
        # (stable, not re-sorted by score) so downstream prompt text reads
        # the way the paradigm author declared it.
        selected = [skill_id for idx, skill_id in enumerate(allowlist) if idx in selected_idx]
        return selected, True, [(skill_id, score) for score, _, skill_id in top]

    @staticmethod
    def _parse_tool_call(tc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        return name, args

    async def _invoke_tool(
        self,
        tc: dict[str, Any],
        *,
        tool_context: dict[str, Any],
        semaphore: asyncio.Semaphore | None,
        cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], bool]:
        name, args = self._parse_tool_call(tc)
        from sciteam.wizard_tools import run_research_tool, tool_deterministic

        # P1-7: reuse a same-work-item (tool, args) result instead of
        # re-invoking — only for catalog `deterministic: true` tools (e.g.
        # literature_search, run_eval), never for web_search/submit_job
        # where a repeat call may legitimately return something different.
        # This is the concrete fix for "thinking loops in place": a model
        # that re-issues an identical query gets the same answer instantly
        # rather than burning a tool round (and, for side-effect-adjacent
        # deterministic tools, re-triggering real work).
        cache_key: tuple[str, str] | None = None
        if cache is not None and tool_deterministic(name):
            cache_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            existing = cache.get(cache_key)
            if existing is not None:
                # Single-flight: if the *first* identical call is still
                # in-flight (e.g. two identical calls dispatched together
                # in the same concurrent `readonly` batch), await its
                # future instead of racing a duplicate invocation — the
                # get/set below has no `await` between them, so this check
                # is race-free on asyncio's single-threaded event loop.
                result = await existing if isinstance(existing, asyncio.Future) else existing
                return tc, name, args, result, True
            pending: asyncio.Future = asyncio.get_event_loop().create_future()
            cache[cache_key] = pending

        if semaphore is not None:
            async with semaphore:
                result = await asyncio.to_thread(
                    run_research_tool, name, args, context=tool_context
                )
        else:
            result = await asyncio.to_thread(run_research_tool, name, args, context=tool_context)
        if cache_key is not None:
            cache[cache_key] = result
            if not pending.done():
                pending.set_result(result)
        return tc, name, args, result, False

    async def _run_tool_calls(
        self,
        calls: list[dict[str, Any]],
        *,
        tool_context: dict[str, Any],
        cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> list[tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], bool]]:
        """Execute one round's tool calls: consecutive `readonly` calls run
        concurrently (bounded by `_max_parallel_readonly`); every other
        permission class (`side_effect`, `privileged`, unknown) runs
        serially, in the model's original order. Results are always
        returned in the original call order regardless of execution order,
        so the trace/messages stay deterministic and replayable. The final
        tuple element is `cache_hit` (P1-7 dedup cache).
        """
        from sciteam.wizard_tools import tool_permission

        results: list[tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], bool]] = []
        i = 0
        n = len(calls)
        while i < n:
            name0, _ = self._parse_tool_call(calls[i])
            readonly = tool_permission(name0) == "readonly"
            j = i + 1
            while j < n:
                namej, _ = self._parse_tool_call(calls[j])
                if (tool_permission(namej) == "readonly") != readonly:
                    break
                j += 1
            group = calls[i:j]
            if readonly and len(group) > 1:
                semaphore = asyncio.Semaphore(self._max_parallel_readonly)
                group_results = await asyncio.gather(
                    *[
                        self._invoke_tool(
                            tc, tool_context=tool_context, semaphore=semaphore, cache=cache
                        )
                        for tc in group
                    ]
                )
            else:
                group_results = [
                    await self._invoke_tool(
                        tc, tool_context=tool_context, semaphore=None, cache=cache
                    )
                    for tc in group
                ]
            results.extend(group_results)
            i = j
        return results

    def _archive_tool_result(
        self,
        tool_context: dict[str, Any],
        *,
        tool_round: int,
        call_id: str,
        content: str,
    ) -> str:
        workspace = Path(str(tool_context.get("mission_workspace") or "."))
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", call_id)[:80] or "tool"
        try:
            path = persist_full_text(
                workspace / "tool_history", f"{tool_round}_{safe_id}.json", content
            )
            return str(path)
        except OSError:
            return "<disk write failed>"

    def _compact_tool_history(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_context: dict[str, Any],
        inner_loop: list[dict[str, Any]],
        tool_round: int,
    ) -> None:
        """P1-6: fold older `role: "tool"` messages into a one-line digest
        once their combined size passes `_tool_history_budget_chars`, so a
        long research loop does not resend megabytes of stale tool JSON on
        every remaining round. The most recent `_TOOL_HISTORY_KEEP_RECENT`
        tool messages are left untouched (still directly usable); the rest
        get their full content persisted to disk and replaced in-place with
        a short pointer. Mutates `messages` in place; deterministic (order
        and selection depend only on message count/size, not on content).
        """
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        total_chars = sum(len(str(messages[i].get("content") or "")) for i in tool_indices)
        if total_chars <= self._tool_history_budget_chars:
            return
        compactable = tool_indices[:-_TOOL_HISTORY_KEEP_RECENT] if tool_indices else []
        compactable = [
            i
            for i in compactable
            if not str(messages[i].get("content") or "").startswith("[compacted]")
        ]
        if not compactable:
            return
        workspace = Path(str(tool_context.get("mission_workspace") or "."))
        history_dir = workspace / "tool_history"
        chars_saved = 0
        n_compacted = 0
        for i in compactable:
            msg = messages[i]
            content = str(msg.get("content") or "")
            if len(content) < 500:
                continue  # not worth a disk round-trip
            call_id = str(msg.get("tool_call_id") or f"msg{i}")
            digest_path = history_dir / f"{tool_round}_{call_id}.json"
            try:
                history_dir.mkdir(parents=True, exist_ok=True)
                digest_path.write_text(content, encoding="utf-8")
                pointer = str(digest_path)
            except OSError:
                pointer = "<disk write failed>"
            summary = content[:160].replace("\n", " ")
            msg["content"] = (
                f"[compacted] {len(content)} chars folded (audit copy: {pointer}) — head: {summary}"
            )
            chars_saved += len(content) - len(msg["content"])
            n_compacted += 1
        if n_compacted:
            inner_loop.append(
                {
                    "kind": "tool_history_compact",
                    "tool_round": tool_round,
                    "messages_compacted": n_compacted,
                    "chars_saved": chars_saved,
                    "kept_recent": _TOOL_HISTORY_KEEP_RECENT,
                    "budget_chars": self._tool_history_budget_chars,
                }
            )

    async def _complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]],
        tool_context: dict[str, Any],
        tool_trace: list[dict[str, Any]],
        inner_loop: list[dict[str, Any]],
        max_tool_rounds: int | None = None,
    ):
        """LLM↔tool cycle for one work item (agent inner loop).

        ``inner_loop`` records every LLM turn and tool call for Observatory replay.
        ``tool_trace`` keeps the legacy tool-only list for summary notes.
        """
        if max_tool_rounds is None:
            max_tool_rounds = self._max_tool_rounds
        max_tool_rounds = max(1, int(max_tool_rounds))
        # P1-7: scoped to this one work item's inner loop only (fresh per
        # call, never an instance attribute) so concurrent work items never
        # share or race on cached tool results.
        tool_result_cache: dict[tuple[str, str], dict[str, Any]] = {}

        if not tool_definitions:
            resp = await self._client.acomplete(
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_format=_JSON_OBJECT_RESPONSE_FORMAT,
            )
            inner_loop.append(
                {
                    "kind": "llm_turn",
                    "tool_round": 0,
                    "n_tool_calls": 0,
                    "content_preview": str(resp.content or "")[:800],
                    "finish_reason": "final_no_tools",
                }
            )
            return resp

        for tool_round in range(max_tool_rounds + 1):
            resp = await self._client.acomplete(
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                tools=tool_definitions,
                tool_choice="auto",
            )
            n_calls = len(resp.tool_calls or [])
            inner_loop.append(
                {
                    "kind": "llm_turn",
                    "tool_round": tool_round,
                    "n_tool_calls": n_calls,
                    "content_preview": str(resp.content or "")[:800],
                    "finish_reason": "final" if not resp.tool_calls else "tool_calls",
                }
            )
            if not resp.tool_calls:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "Use no more tools. Return the final JSON envelope only.",
                    }
                )
                final = await self._client.acomplete(
                    messages,
                    temperature=0.2,
                    max_tokens=self._max_tokens,
                    response_format=_JSON_OBJECT_RESPONSE_FORMAT,
                )
                inner_loop.append(
                    {
                        "kind": "llm_turn",
                        "tool_round": tool_round,
                        "n_tool_calls": 0,
                        "content_preview": str(final.content or "")[:800],
                        "finish_reason": "structured_final",
                    }
                )
                return final
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or None,
                    "tool_calls": resp.tool_calls,
                }
            )
            calls = [tc for tc in resp.tool_calls if isinstance(tc, dict)]
            for tc, name, args, result, cache_hit in await self._run_tool_calls(
                calls, tool_context=tool_context, cache=tool_result_cache
            ):
                full_text = json.dumps(result, ensure_ascii=False)
                call_id = str(tc.get("id") or name)
                archive_path = self._archive_tool_result(
                    tool_context, tool_round=tool_round, call_id=call_id, content=full_text
                )
                preview = full_text[: self._tool_result_preview_chars]
                visible = full_text
                if len(full_text) > self._tool_result_visible_chars:
                    visible = (
                        full_text[: self._tool_result_visible_chars]
                        + f"\n[truncated {len(full_text)} chars; audit copy: {archive_path}]"
                    )
                tool_row = {
                    "kind": "tool_call",
                    "tool_round": tool_round,
                    "tool": name,
                    "arguments": args,
                    "result_preview": preview,
                    "result_chars": len(full_text),
                    "result_archived": archive_path,
                    "cache_hit": cache_hit,
                }
                tool_trace.append(
                    {
                        "tool": name,
                        "arguments": args,
                        "result_preview": preview,
                        "result_archived": archive_path,
                    }
                )
                inner_loop.append(tool_row)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": visible,
                    }
                )
            self._compact_tool_history(
                messages,
                tool_context=tool_context,
                inner_loop=inner_loop,
                tool_round=tool_round,
            )
        messages.append(
            {
                "role": "user",
                "content": "Stop calling tools. Emit the final JSON envelope only.",
            }
        )
        final = await self._client.acomplete(
            messages,
            temperature=0.2,
            max_tokens=self._max_tokens,
            response_format=_JSON_OBJECT_RESPONSE_FORMAT,
        )
        inner_loop.append(
            {
                "kind": "llm_turn",
                "tool_round": max_tool_rounds + 1,
                "n_tool_calls": 0,
                "content_preview": str(final.content or "")[:800],
                "finish_reason": "forced_final",
            }
        )
        return final

    async def _repair_contract_violations(
        self,
        messages: list[dict[str, Any]],
        *,
        envelope: dict[str, Any],
        exit_contract: str,
        inner_loop: list[dict[str, Any]],
        mission_id: str = "",
    ) -> dict[str, Any]:
        """P1-7: preemptively validate `envelope["artifact"]` against the
        engine's own `ContractValidator` and, on violations, feed the exact
        schema-validator error text back for a targeted re-emit. This
        replaces a "blind retry" (generic "invalid JSON, try again", no
        information about *what* was wrong) with the same validator the
        engine uses downstream, so worker-side pre-checks and engine-side
        contract enforcement can never disagree about what "valid" means.

        Validates the *same transform* the write path applies just after
        this call returns (`sanitize_artifact_payload` + `mission_id`
        provenance default) — not the raw model artifact — otherwise this
        would flag violations (missing `mission_id`, stray `_`-prefixed
        notes, null optionals) that the real write path already fixes,
        and burn repair round-trips on non-problems.

        Best-effort and bounded: if repairs run out, the last envelope is
        returned as-is and the engine's own contract check still has the
        final say — this never changes who owns contract enforcement, it
        only saves wasted mission rounds on schema mistakes the model can
        fix immediately once told exactly what's wrong.
        """
        from sciteam.mission import ContractValidator

        validator = ContractValidator(self._schemas_dir)
        working = messages
        for attempt in range(_MAX_CONTRACT_REPAIR_ATTEMPTS + 1):
            artifact = envelope.get("artifact")
            if not isinstance(artifact, dict):
                return envelope
            payload = sanitize_artifact_payload(dict(artifact))
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("mission_id", mission_id)
            errors = validator.validate(exit_contract, payload)
            if not errors:
                if attempt:
                    inner_loop.append(
                        {
                            "kind": "contract_repair",
                            "attempt": attempt,
                            "exit_contract": exit_contract,
                            "errors": [],
                            "resolved": True,
                        }
                    )
                return envelope
            if attempt == _MAX_CONTRACT_REPAIR_ATTEMPTS:
                inner_loop.append(
                    {
                        "kind": "contract_repair",
                        "attempt": attempt,
                        "exit_contract": exit_contract,
                        "errors": errors[:20],
                        "resolved": False,
                        "exhausted": True,
                    }
                )
                return envelope
            inner_loop.append(
                {
                    "kind": "contract_repair",
                    "attempt": attempt,
                    "exit_contract": exit_contract,
                    "errors": errors[:20],
                }
            )
            repair_msg = (
                f"Your `artifact` object fails contract `{exit_contract}` with these "
                "schema-validator errors:\n"
                + "\n".join(f"- {e}" for e in errors[:20])
                + "\nRe-emit the FULL JSON envelope again with `artifact` fixed to "
                "satisfy every listed violation. Keep summary/facts/files as-is "
                "unless they must change to fix the errors."
            )
            working = working + [
                {"role": "assistant", "content": json.dumps(envelope, ensure_ascii=False)},
                {"role": "user", "content": repair_msg},
            ]
            response = await self._client.acomplete(
                working, temperature=0.2, max_tokens=self._max_tokens
            )
            repaired = _extract_json(response.content)
            if not isinstance(repaired, dict):
                inner_loop.append(
                    {
                        "kind": "contract_repair",
                        "attempt": attempt,
                        "exit_contract": exit_contract,
                        "errors": ["repair reply was not a valid JSON envelope"],
                        "resolved": False,
                        "exhausted": True,
                    }
                )
                return envelope
            envelope = repaired
        return envelope

    def _fit_to_context(
        self,
        *,
        agent_key: str,
        pins: list[str],
        history: list[dict[str, Any]],
        seat_memory: list[dict[str, Any]],
        kb_digest: dict[str, Any],
        mission_inputs: dict[str, Any],
        skills_allowlist: list[str],
        fixed_overhead_chars: int,
    ) -> dict[str, Any]:
        """P0-4: size-aware prompt assembly instead of independently-fixed
        per-section clip constants (50k kb_digest / 200k mission_inputs / 16k
        per skill / role compaction profile) that never talk to each other or
        to the model's actual `context_window`.

        Degrades in a fixed order — each step is the least information-dense
        section first — recomputing the estimate after every step, and stops
        as soon as the estimate fits (or the floor is hit). Deterministic:
        same inputs always produce the same degrade steps.
        """
        context_window = int(
            getattr(getattr(self._client, "config", None), "context_window", limits.CONTEXT_WINDOW)
            or limits.CONTEXT_WINDOW
        )
        # Conservative (i.e. deliberately low) chars-per-token so the char
        # budget under-, not over-, estimates what actually fits.
        chars_per_token = 3
        headroom_tokens = 2000
        budget_chars = max(
            20_000, (context_window - self._max_tokens - headroom_tokens) * chars_per_token
        )

        kb_budget = 50_000
        mission_budget = 200_000
        skill_max_chars = self._assets.max_skill_chars
        pack_scale = 1.0
        steps: list[str] = []

        def _pack_text() -> str:
            return build_working_pack(
                agent_key=agent_key,
                pins=pins,
                task_history=history,
                seat_memory=seat_memory,
                scale=pack_scale,
            ).render()

        def _skill_text() -> str:
            return (
                self._assets.skill_pack(skills_allowlist, max_chars_override=skill_max_chars)
                if skills_allowlist
                else ""
            )

        def _estimate(pack_text: str, skill_text: str) -> int:
            kb_len = (
                min(len(json.dumps(kb_digest, ensure_ascii=False)), kb_budget) if kb_digest else 0
            )
            mission_len = (
                min(len(json.dumps(mission_inputs, ensure_ascii=False)), mission_budget)
                if mission_inputs
                else 0
            )
            return fixed_overhead_chars + kb_len + mission_len + len(skill_text) + len(pack_text)

        skill_text = _skill_text()
        pack_text = _pack_text()
        total = _estimate(pack_text, skill_text)

        if total > budget_chars and kb_digest:
            kb_budget = 25_000
            steps.append("kb_digest:50000->25000")
            total = _estimate(pack_text, skill_text)

        if total > budget_chars and mission_inputs:
            mission_budget = 60_000
            steps.append("mission_inputs:200000->60000")
            total = _estimate(pack_text, skill_text)

        if total > budget_chars and pack_scale > 0.25:
            pack_scale = 0.5
            steps.append("working_pack:scale=0.5")
            pack_text = _pack_text()
            total = _estimate(pack_text, skill_text)

        if total > budget_chars and skills_allowlist:
            skill_max_chars = max(4000, skill_max_chars // 2)
            steps.append(f"skills:{self._assets.max_skill_chars}->{skill_max_chars}/skill")
            skill_text = _skill_text()
            total = _estimate(pack_text, skill_text)

        if total > budget_chars and pack_scale > 0.25:
            pack_scale = 0.25
            steps.append("working_pack:scale=0.25")
            pack_text = _pack_text()
            total = _estimate(pack_text, skill_text)

        return {
            "context_window": context_window,
            "budget_chars": budget_chars,
            "estimated_chars": total,
            "over_budget": total > budget_chars,
            "degraded": bool(steps),
            "steps": steps,
            "kb_digest_budget": kb_budget,
            "mission_inputs_budget": mission_budget,
            "skill_max_chars": skill_max_chars,
            "pack_scale": pack_scale,
            "pack_text": pack_text,
            "skill_text": skill_text,
        }

    async def run_subagent(
        self,
        *,
        agent_id: str,
        task: str,
        work_dir: str,
        parent_run_id: str | None = None,
        depth: int = 0,
        context: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        del depth
        context = context or {}
        agent_key = str(context.get("team_agent_key") or agent_id)
        team_run_id = str(context.get("team_run_id") or "")
        meta = dict(context.get("team_metadata") or {})
        mission_id = str(meta.get("mission_id") or "")
        exit_contract = str(meta.get("exit_contract") or "")
        skills_allowlist = [str(s) for s in (meta.get("skills_allowlist") or [])]
        if skills_allowlist and self._assets.pack_dir is not None:
            try:
                ensure_skills_in_pack(Path(self._assets.pack_dir).parent, skills_allowlist)
            except PackError as exc:
                raise PackError(f"allowlist skill missing from pack (DEF-NEW-16): {exc}") from exc
        artifact_path = str(meta.get("artifact_path") or "")
        mission_inputs = meta.get("mission_inputs") or {}
        kb_digest = meta.get("kb_digest") or {}
        # verifier-owned missions: the artifact file may ONLY be created by the
        # injected verifier (run_eval); hand-written envelopes are rejected.
        verifier_owned = str(meta.get("artifact_source") or "") == "verifier"
        artifact_gate = meta.get("artifact_gate")
        round_hint = str(meta.get("next_round_hint") or "")

        pins = [
            f"mission: {mission_id} · exit contract: {exit_contract}",
            "official numbers may only come from the frozen eval script; never invent metrics",
        ]
        work_item_id = str(context.get("work_item_id") or "")
        history = self._task_history.get(team_run_id, [])
        # Keep the current card plus bounded same-seat outcomes across waves.
        if work_item_id:
            history = [
                h
                for h in history
                if str(h.get("work_item_id") or "") in {"", work_item_id}
                or str(h.get("agent_key") or "") == agent_key
            ]
        # P1-5: rank same-seat memory by lexical overlap with the current
        # task instead of plain recency, and fold in bounded cross-mission
        # role memory when the campaign runner wired a shared path.
        seat_memory = SeatMemoryStore(work_dir).recall(query=task)
        campaign_memory_path = str(meta.get("campaign_memory_path") or "")
        if campaign_memory_path:
            seen = {(m.get("work_item_id"), m.get("created_at")) for m in seat_memory}
            for row in CampaignMemoryStore(campaign_memory_path).recall(
                _role_key(agent_key), query=task
            ):
                key = (row.get("work_item_id"), row.get("created_at"))
                if key not in seen:
                    seen.add(key)
                    seat_memory.append({**row, "scope": "campaign_role"})

        artifact_duty = self._is_artifact_role(agent_key, meta)
        assessment_mode = bool(meta.get("assessment_mode")) or self._is_assess_role(agent_key, meta)
        required_capabilities = self._required_tool_capabilities(skills_allowlist, mission_id)
        effective_capabilities = worker_common.effective_tool_capabilities(required_capabilities)
        has_bash = "compute.bash" in effective_capabilities
        envelope_instructions = (
            ASSESS_ENVELOPE_INSTRUCTIONS if assessment_mode else ENVELOPE_INSTRUCTIONS
        ) + (_BASH_TOOL_CLAUSE if has_bash else "")
        constitution_text = self._assets.constitution()
        role_prompt_text = self._assets.role_prompt(agent_key)

        # Task text already carries duty/deliverable; put it first so the model
        # does not drown in schema/meta before knowing what this seat must do.
        user_parts = [
            f"# Task card (authoritative for this claim)\n{task}",
            (
                f"# Seat\nagent_key: {agent_key}\n"
                + (
                    "You are the round assessor. Return round_assessment JSON only "
                    "(decision + reason_code). Do not emit the exit artifact."
                    if assessment_mode
                    else (
                        "You ARE the exit-artifact emitter when ready "
                        "(envelope.artifact → canonical file)."
                        if artifact_duty
                        else "You are NOT the emitter. Deliver via summary + facts only; "
                        "another seat writes the exit file."
                    )
                )
            ),
        ]
        if artifact_duty:
            user_parts.append(
                f"# Exit contract (JSON Schema — you must satisfy this)\n"
                f"```json\n{self._contract_schema_text(exit_contract)}\n```"
            )
            if isinstance(artifact_gate, dict) and artifact_gate.get("pointer"):
                user_parts.append(
                    "# Completion gate\n"
                    "The mission completes only when the exit artifact satisfies "
                    f"`{json.dumps(artifact_gate)}`. Never fake the gate."
                )
        else:
            user_parts.append(
                "# Exit contract\n"
                f"Schema `{exit_contract}` is assembled by the emit-duty seat. "
                "Do not paste a full artifact object unless explicitly repairing a draft."
            )
        if verifier_owned:
            user_parts.append(
                "# VERIFIER-OWNED ARTIFACT\n"
                "This mission's exit artifact may ONLY be produced by the frozen "
                "verifier via `run_eval`. Hand-written `artifact` objects will be "
                "rejected and logged as integrity violations."
            )
        if round_hint:
            user_parts.append(f"# Coordinator hint from last round\n{round_hint}")

        # Everything above is non-degradable (task/schema/gate framing); size
        # it once so the budget fit below reflects real fixed overhead.
        fixed_overhead_chars = (
            len(constitution_text)
            + len(role_prompt_text)
            + len(envelope_instructions)
            + sum(len(p) for p in user_parts)
        )
        # P1-8: which skill BODIES get injected into the prompt is a subset
        # (top-k by task overlap) when the allowlist is large; which
        # CAPABILITIES the seat is granted (below, `_required_tool_capabilities`)
        # always reads the full `skills_allowlist` — selection here never
        # narrows the capability surface.
        injected_skills, topk_applied, topk_scores = self._select_topk_skills(
            task, skills_allowlist
        )
        inner_loop_skills_row: dict[str, Any] = {
            "kind": "skills_selected",
            "allowlist_size": len(skills_allowlist),
            "injected": injected_skills,
            "topk_applied": topk_applied,
        }
        if topk_applied:
            inner_loop_skills_row["scores"] = topk_scores
        fit = self._fit_to_context(
            agent_key=agent_key,
            pins=pins,
            history=history,
            seat_memory=seat_memory,
            kb_digest=kb_digest,
            mission_inputs=mission_inputs,
            skills_allowlist=injected_skills,
            fixed_overhead_chars=fixed_overhead_chars,
        )

        system_parts = [constitution_text, role_prompt_text]
        if skills_allowlist:
            system_parts.append(
                "# Skills you MUST follow (free-form work is a policy violation "
                "when a skill applies)\n\n" + fit["skill_text"]
            )
        system_parts.append(envelope_instructions)
        system = "\n\n---\n\n".join(part for part in system_parts if part.strip())

        if mission_inputs:
            user_parts.append(
                "# Mission inputs\n"
                + _render_inputs_budgeted(mission_inputs, fit["mission_inputs_budget"])
            )
        if kb_digest:
            user_parts.append(
                "# Campaign knowledge digest\n```json\n"
                + json.dumps(kb_digest, ensure_ascii=False, indent=2)[: fit["kb_digest_budget"]]
                + "\n```"
            )
        user_parts.append(f"# Team context\n{fit['pack_text']}")
        user = "\n\n".join(user_parts)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        tool_trace: list[dict[str, Any]] = []
        inner_loop: list[dict[str, Any]] = [
            inner_loop_skills_row,
            {
                "kind": "context_budget",
                "context_window": fit["context_window"],
                "budget_chars": fit["budget_chars"],
                "estimated_chars": fit["estimated_chars"],
                "degraded": fit["degraded"],
                "steps": fit["steps"],
            },
            {
                "kind": "awaiting_llm",
                "note": "claimed; waiting on model (tools may follow)",
            },
        ]
        from sciteam.wizard_tools import tool_definitions_for

        # B4: `effective_capabilities` (computed above, alongside `has_bash`,
        # for the envelope-instructions clause) already folds in the
        # always-on file-workspace tools and, when justified, `compute.bash`
        # — the same predicate PiRuntime uses, so both arms grant an
        # identical tool surface for an identical allowlist.
        tool_definitions = tool_definitions_for(effective_capabilities)
        enable_tools = bool(tool_definitions)
        # Cap tool thrash for non-emit analysis seats (ranking/reflection/…).
        tool_budget = self._max_tool_rounds
        if enable_tools and not artifact_duty:
            tool_budget = min(tool_budget, 6)
        if work_item_id:
            try:
                from sciteam.persist import append_inner_loop_trace

                append_inner_loop_trace(
                    work_dir,
                    work_item_id=work_item_id,
                    steps=inner_loop,
                    meta={
                        "agent_key": agent_key,
                        "team_run_id": team_run_id,
                        "mission_id": mission_id,
                        "phase": "awaiting_llm",
                    },
                )
            except OSError:
                pass
        # 2026-08-15 audit fix: an assess-duty seat's job is to independently
        # verify facts about the mission (including cross-referencing paths
        # inside an artifact it did not author, e.g. package_manifest.json's
        # contents[] pointing at campaign-root files like protocol.yaml) —
        # confining it to its own mission subdirectory made every such check
        # either impossible (silently never attempted) or a guaranteed false
        # "missing" verdict. Production/emit seats are unaffected and stay
        # confined to their own mission_workspace.
        campaign_run_dir = str(meta.get("campaign_run_dir") or "")
        mission_workspace = (
            campaign_run_dir
            if assessment_mode and campaign_run_dir
            else str(Path(artifact_path).parent if artifact_path else Path(work_dir))
        )
        response = await self._complete_with_tools(
            messages,
            tool_definitions=tool_definitions,
            tool_context={
                "mission_workspace": mission_workspace,
                "team_run_id": team_run_id,
                "work_item_id": work_item_id,
                "agent_key": agent_key,
                "allow_privileged": False,
            },
            tool_trace=tool_trace,
            inner_loop=inner_loop,
            max_tool_rounds=tool_budget,
        )
        envelope = _extract_json(response.content)
        if envelope is None or (assessment_mode and not _assessment_envelope_ok(envelope)):
            repair_msg = (
                "Re-emit ONLY round_assessment JSON with keys decision, reason_code, "
                "summary, next_round_hint, assessor_key. No markdown."
                if assessment_mode
                else (
                    "Your reply was not a single valid JSON object. Re-emit ONLY "
                    "the JSON envelope now."
                )
            )
            repair = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": repair_msg},
            ]
            response = await self._client.acomplete(
                repair, temperature=0.2, max_tokens=self._max_tokens
            )
            envelope = _extract_json(response.content)
        if envelope is None:
            diagnosis = (
                f"worker {agent_key} failed to produce a JSON envelope after repair; "
                f"last counterexample={str(response.content or '')[:1200]}"
            )
            self._commit_failure_memory(
                team_run_id=team_run_id,
                agent_key=agent_key,
                work_dir=work_dir,
                context=context,
                task=task,
                reason_code="invalid_json_envelope",
                diagnosis=diagnosis,
                counterexample=str(response.content or ""),
            )
            try:
                write_agent_result_file(
                    work_dir,
                    round_index=int(context.get("round_index") or 0),
                    content=diagnosis,
                )
                if artifact_path:
                    append_agent_log(
                        Path(artifact_path).parent,
                        {
                            "mission_id": mission_id,
                            "team_run_id": team_run_id,
                            "agent_key": agent_key,
                            "round_index": context.get("round_index", 0),
                            "wrote_artifact": False,
                            "summary": diagnosis,
                            "reason_code": "invalid_json_envelope",
                            "recoverable": True,
                            "usage": response.usage,
                        },
                    )
            except OSError:
                pass
            failed_run = Run(
                run_id=f"llm_{agent_key}_{len(self.transcript) + 1}",
                spec=RunSpec(
                    kind="team_worker",
                    input=task,
                    agent_id=agent_id,
                    parent_run_id=parent_run_id,
                    metadata={"work_dir": work_dir},
                ),
                state=RunState.FAILED,
                output=diagnosis,
                error=diagnosis,
            )
            return AgentRunResult(
                run=failed_run,
                output=diagnosis,
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="invalid_json_envelope",
                diagnosis=diagnosis,
                retry_hint=(
                    "Return exactly one valid JSON object matching the required "
                    "worker envelope; preserve useful partial work."
                ),
            )
        if assessment_mode and not _assessment_envelope_ok(envelope):
            diagnosis = f"assessor {agent_key} failed to produce decision/reason_code envelope"
            self._commit_failure_memory(
                team_run_id=team_run_id,
                agent_key=agent_key,
                work_dir=work_dir,
                context=context,
                task=task,
                reason_code="illegal_assessment",
                diagnosis=diagnosis,
                counterexample=json.dumps(envelope, ensure_ascii=False)[:4000],
            )
            failed_run = Run(
                run_id=f"llm_{agent_key}_{len(self.transcript) + 1}",
                spec=RunSpec(
                    kind="team_worker",
                    input=task,
                    agent_id=agent_id,
                    parent_run_id=parent_run_id,
                    metadata={"work_dir": work_dir},
                ),
                state=RunState.FAILED,
                output=diagnosis,
                error=diagnosis,
            )
            return AgentRunResult(
                run=failed_run,
                output=diagnosis,
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="illegal_assessment",
                diagnosis=diagnosis,
                retry_hint=(
                    "Re-emit round_assessment JSON with decision, reason_code, "
                    "summary, next_round_hint, and assessor_key."
                ),
            )

        if artifact_duty and exit_contract and isinstance(envelope.get("artifact"), dict):
            envelope = await self._repair_contract_violations(
                messages,
                envelope=envelope,
                exit_contract=exit_contract,
                inner_loop=inner_loop,
                mission_id=mission_id,
            )

        summary = str(envelope.get("summary") or "")
        if tool_trace:
            used = ", ".join(sorted({t["tool"] for t in tool_trace}))
            note = f"[research tools used: {used}]"
            summary = f"{summary}\n{note}" if summary else note
        facts = [str(f) for f in (envelope.get("facts") or [])]
        artifact = envelope.get("artifact")
        mission_dir = Path(artifact_path).parent if artifact_path else Path(work_dir)
        mission_dir.mkdir(parents=True, exist_ok=True)
        if work_item_id and inner_loop:
            with contextlib.suppress(OSError):
                append_inner_loop_trace(
                    work_dir,
                    work_item_id=work_item_id,
                    steps=inner_loop,
                    meta={
                        "agent_key": agent_key,
                        "team_run_id": team_run_id,
                        "mission_id": mission_id,
                        "round_index": context.get("round_index", 0),
                        "tools_enabled": enable_tools,
                        "n_tool_calls": len(tool_trace),
                        "max_tool_rounds": self._max_tool_rounds,
                    },
                    mission_dir=mission_dir if artifact_path else None,
                )

        written_files: list[str] = []
        files = envelope.get("files")
        if isinstance(files, dict):
            for name, content in files.items():
                safe = Path(str(name)).name  # flatten: no traversal, mission dir only
                if not safe or not isinstance(content, str):
                    continue
                (mission_dir / safe).write_text(content, encoding="utf-8")
                written_files.append(safe)

        eval_note = ""
        eval_wrote_artifact = False
        run_eval = envelope.get("run_eval")
        if isinstance(run_eval, dict) and self._eval_runner is not None:
            problem_id = str(run_eval.get("problem_id") or "")
            candidate_name = str(run_eval.get("candidate_file") or "")
            out_name = Path(str(run_eval.get("out") or "results.json")).name
            out_path = mission_dir / out_name
            seed = run_eval.get("seed")
            # 2026-08-13 audit finding (Line D held_in/held_out): a natural-
            # language "use seed=N" instruction in the task topic is not
            # enforced anywhere — the LLM is free to omit or misstate it, and
            # ~50% of held_out build_fix_loop evals were found to have silently
            # reused the held_in seed. When the mission declares a
            # required_seed, it is authoritative and overrides whatever the
            # model put in run_eval; a mismatch is only logged, never trusted.
            required_seed = meta.get("required_seed")
            if required_seed is not None:
                try:
                    required_seed = int(required_seed)
                except (TypeError, ValueError):
                    required_seed = None
            seed_override_note = ""
            if required_seed is not None:
                if seed is not None and int(seed) != required_seed:
                    seed_override_note = (
                        f"[run_eval requested seed={seed!r} but mission requires "
                        f"seed={required_seed}; overriding to the required seed]\n"
                    )
                seed = required_seed
            candidate = mission_dir / Path(candidate_name).name if candidate_name else None
            try:
                if candidate is not None and not candidate.is_file():
                    raise FileNotFoundError(f"candidate file not found: {candidate.name}")
                self._eval_runner(
                    problem_id,
                    candidate,
                    out_path,
                    int(seed) if seed is not None else None,
                )
                if candidate is not None and candidate.is_file():
                    # archive the exact evaluated source under its hash so the
                    # recorded results stay replayable even if the working file
                    # is overwritten in later rounds
                    import hashlib

                    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                    archive = mission_dir / f"evaluated_{digest[:12]}.py"
                    if not archive.exists():
                        archive.write_bytes(candidate.read_bytes())
                eval_note = (
                    seed_override_note
                    + f"[frozen eval ran: {problem_id} -> {out_path.name}]\n"
                    + (out_path.read_text(encoding="utf-8")[:2000] if out_path.is_file() else "")
                )
                eval_wrote_artifact = (
                    bool(artifact_path) and out_path == Path(artifact_path) and out_path.is_file()
                )
            except Exception as exc:  # noqa: BLE001 — evaluator must see failures to iterate
                eval_note = seed_override_note + f"[frozen eval FAILED: {exc}]"

        wrote_artifact = False
        if eval_wrote_artifact:
            # The frozen eval IS the artifact source; never overwrite it by hand.
            wrote_artifact = True
        elif verifier_owned and isinstance(artifact, dict):
            output_note = (
                "[REJECTED: this mission's artifact is verifier-owned; hand-written "
                "artifacts are an integrity violation. Use run_eval instead.]"
            )
            summary = f"{summary}\n{output_note}" if summary else output_note
        elif isinstance(artifact, dict) and artifact_path and not artifact_duty:
            # Never silently drop — empty CONTINUE loops follow when emit is ignored.
            if self._is_assess_role(agent_key, meta):
                output_note = (
                    "[REJECTED: assess seats may not write the exit artifact; "
                    "return a round_assessment envelope only.]"
                )
            else:
                duty = sorted(self._artifact_duty_tokens(meta)) or ["(none declared in paradigm)"]
                output_note = (
                    "[REJECTED: role "
                    f"`{agent_key}` is not an artifact-duty seat; "
                    f"only {duty} may emit the exit artifact. Put partial work in "
                    "summary/facts/files (e.g. *_draft.json), not envelope.artifact.]"
                )
            summary = f"{summary}\n{output_note}" if summary else output_note
        elif artifact_duty and isinstance(artifact, dict) and artifact_path:
            payload = sanitize_artifact_payload(dict(artifact))
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("mission_id", mission_id)
            Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
            Path(artifact_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            wrote_artifact = True

        if assessment_mode:
            assess_payload = {
                "decision": str(envelope.get("decision") or "").strip().lower(),
                "reason_code": str(envelope.get("reason_code") or "").strip(),
                "summary": summary,
                "next_round_hint": str(envelope.get("next_round_hint") or ""),
                "assessor_key": str(envelope.get("assessor_key") or agent_key),
            }
            if "approve_promote_draft" in envelope:
                assess_payload["approve_promote_draft"] = bool(
                    envelope.get("approve_promote_draft")
                )
            if "preferred_agent" in envelope:
                assess_payload["preferred_agent"] = envelope.get("preferred_agent")
            output = json.dumps(assess_payload, ensure_ascii=False)
        else:
            output_lines = [summary]
            if facts:
                output_lines.append("facts: " + " | ".join(facts))
            if written_files:
                output_lines.append(f"[files written: {', '.join(written_files)}]")
            if eval_note:
                output_lines.append(eval_note)
            if wrote_artifact:
                output_lines.append(f"[artifact written: {Path(artifact_path).name}]")
            output = "\n".join(line for line in output_lines if line)

        history = self._task_history.setdefault(team_run_id, [])
        history.append(
            {
                "agent_key": agent_key,
                "round_index": context.get("round_index", 0),
                "work_item_id": str(context.get("work_item_id") or ""),
                "state": "done",
                "result": output,
                "error": None,
            }
        )
        try:
            if int(context.get("retry_count") or 0) > 0 or context.get("retry_diagnosis"):
                self._commit_memory_record(
                    work_dir=work_dir,
                    agent_key=agent_key,
                    meta=meta,
                    record=MemoryRecord(
                        outcome="success_after_repair",
                        work_item_id=str(context.get("work_item_id") or ""),
                        round_index=int(context.get("round_index") or 0),
                        counterexample=str(context.get("retry_diagnosis") or "")[:4000],
                        effective_fix=output[:4000],
                        evidence_refs=(
                            f"work_item:{context.get('work_item_id') or ''}",
                            f"round:{context.get('round_index') or 0}",
                        ),
                        reason_code="repair_succeeded",
                    ),
                )
            else:
                # P1-5: ordinary (non-repair) success is also worth
                # remembering — e.g. "this method worked cleanly" — not just
                # failures. Both stores self-compact, so this does not grow
                # seat_memory.jsonl unbounded.
                self._commit_memory_record(
                    work_dir=work_dir,
                    agent_key=agent_key,
                    meta=meta,
                    record=MemoryRecord(
                        outcome="success",
                        work_item_id=str(context.get("work_item_id") or ""),
                        round_index=int(context.get("round_index") or 0),
                        attempted_method=task[:2000],
                        effective_fix=output[:1000],
                        evidence_refs=(
                            f"work_item:{context.get('work_item_id') or ''}",
                            f"round:{context.get('round_index') or 0}",
                        ),
                        reason_code="ordinary_success",
                    ),
                )
        except OSError:
            pass
        self.transcript.append(
            {
                "mission_id": mission_id,
                "agent_key": agent_key,
                "wrote_artifact": wrote_artifact,
                "usage": response.usage,
            }
        )
        try:
            write_agent_result_file(
                work_dir, round_index=int(context.get("round_index") or 0), content=output
            )
            if artifact_path:
                append_agent_log(
                    Path(artifact_path).parent,
                    {
                        "mission_id": mission_id,
                        "team_run_id": team_run_id,
                        "agent_key": agent_key,
                        "round_index": context.get("round_index", 0),
                        "wrote_artifact": wrote_artifact,
                        "written_files": written_files,
                        "eval_note": eval_note[:2000] if eval_note else "",
                        "summary": summary[:4000],
                        "facts": facts[:20],
                        "skill_versions": {
                            skill_id: dict(provenance)
                            for skill_id, provenance in self._assets.loaded_skill_versions.items()
                            if skill_id in skills_allowlist
                        },
                        "usage": response.usage,
                    },
                )
        except OSError:
            pass

        run = Run(
            run_id=f"llm_{agent_key}_{len(self.transcript)}",
            spec=RunSpec(
                kind="team_worker",
                input=task,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                metadata={"work_dir": work_dir},
            ),
            state=RunState.SUCCEEDED,
            output=output,
        )
        return AgentRunResult(run=run, output=output)
