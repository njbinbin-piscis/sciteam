"""LLM-backed worker implementing RuntimePort.

Each subagent call becomes one (or two, on JSON repair) chat completions:
constitution + role prompt + skill pack + mission brief + compacted team
context. Workers answer in a structured JSON envelope; when the envelope
carries an `artifact` object, it is written to the mission artifact path
(the exit contract is validated by the engine, not by the worker).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import sciteam.limits as limits
from sciteam import worker_common
from sciteam.compact import build_working_pack
from sciteam.experiment_pack import PackError, ensure_skills_in_pack
from sciteam.llm import LlmClient
from sciteam.memory import CampaignMemoryStore, MemoryRecord, SeatMemoryStore
from sciteam.persist import (
    append_agent_log,
    write_agent_result_file,
)
from sciteam.prompt_assets import (
    _BASH_TOOL_CLAUSE,
    _SKILL_TOPK_K,
    _SKILL_TOPK_THRESHOLD,
    ASSESS_ENVELOPE_INSTRUCTIONS,
    ENVELOPE_INSTRUCTIONS,
    PromptAssets,
)
from sciteam.runtime import AgentOutcome, AgentRunResult, Run, RunSpec, RunState
from sciteam.tool_loop import ToolLoopMixin
from sciteam.worker_common import extract_json as _extract_json
from sciteam.worker_common import role_key as _role_key
from sciteam.worker_common import sanitize_artifact_payload

# Inner-loop tool rounds per work item (LLM↔tools until final envelope).
# Override with SCITEAM_MAX_TOOL_ROUNDS. Five was a scaffold cap and is far too low
# for literature / research work items. The default supports research-grade iteration.
logger = logging.getLogger(__name__)


_DEFAULT_MAX_TOOL_ROUNDS = 500

# P0-3: readonly tool calls in the same round (e.g. several literature_search
# calls) run concurrently via this many worker threads; side_effect calls
# always run serially and in order. Override with SCITEAM_MAX_PARALLEL_READONLY.
_DEFAULT_MAX_PARALLEL_READONLY = 4


# P1-6: once accumulated `role: "tool"` message content in a single work
# item's inner loop exceeds this many chars, older tool results are folded
# into a one-line digest and their full JSON is persisted to
# `<mission_workspace>/tool_history/*.json`. The most recent
# `tool_loop._TOOL_HISTORY_KEEP_RECENT` tool messages are always kept verbatim so the
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


class LlmWorkerRuntime(ToolLoopMixin):
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
        session_store: Any = None,
        plugin_registry: Any = None,
        sandbox_profile: str | None = None,
    ) -> None:
        """`eval_runner` is an injected verifier callable
        (problem_id, candidate_path | None, out_path, seed | None) -> None;
        the engine knows nothing about what it verifies.

        Exit-artifact duty is NOT a hardcoded role list here. MissionRunner
        injects ``metadata.artifact_emit_roles`` from the paradigm YAML
        (``emits_exit_artifact: true`` on member seats).

        `session_store` (docs/SESSION-KERNEL.md) is optional and additive:
        when a composition root supplies a `sciteam.session.SessionStore`,
        every inner-loop turn/tool call is also recorded as a hash-chained
        SessionEvent. `None` (the default) preserves this class's exact
        prior behavior — no new required argument, no behavior change for
        any existing caller that does not opt in.
        """
        self._client = client
        self._assets = assets
        self._session_store = session_store
        self._plugin_registry = plugin_registry
        # docs/28 阶段4: `None` defers to `sandbox.resolve_sandbox_profile()`
        # (env > product default `unattended`); explicit callers (tests,
        # CLI `--sandbox-profile development`) may opt into weaker isolation.
        self._sandbox_profile = sandbox_profile
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
        tool_definitions = tool_definitions_for(
            effective_capabilities, plugins=self._plugin_registry
        )
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
                logger.warning("could not append agent log for %s (continuing)", agent_key)
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
                # E5 (docs/SESSION-KERNEL.md §5.1): the run-wide session
                # store, not one reconstructed from mission_workspace — its
                # archive root is a property of the run, not of one seat.
                "session_store": self._session_store,
                "sandbox_profile": self._sandbox_profile,
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
                logger.warning(
                    "could not persist the failed-envelope record for %s (continuing)", agent_key
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
            # E6 (28-... §7.2): `artifact_submitted` is the second (and
            # last) blockable plugin hook. This is the actual commit point
            # of the exit-contract artifact — an authorized seat's
            # institutional authority (`artifact_duty`, checked above) is
            # necessary but, with a plugin installed, no longer sufficient;
            # a plugin can add scrutiny here, never remove it.
            registry = getattr(self, "_plugin_registry", None)
            blocked_reason = ""
            if registry is not None:
                hook_result = registry.emit(
                    "artifact_submitted",
                    {
                        "mission_id": mission_id,
                        "agent_key": agent_key,
                        "artifact_path": artifact_path,
                        "payload": payload,
                    },
                )
                if hook_result.block:
                    blocked_reason = hook_result.reason or "blocked by an installed plugin hook"
            if blocked_reason:
                summary = (
                    f"{summary}\n[REJECTED by plugin hook: {blocked_reason}]"
                    if summary
                    else f"[REJECTED by plugin hook: {blocked_reason}]"
                )
            else:
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
            logger.warning("could not commit seat memory for %s (continuing)", agent_key)
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
            logger.warning("could not write the agent result file for %s (continuing)", agent_key)

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
