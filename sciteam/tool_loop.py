"""The tool-calling half of the reference LLM worker.

``LlmWorkerRuntime`` does two separable things: it assembles a prompt and
interprets the answer (``llm_worker.py``), and it drives the LLM↔tool inner
loop that happens in between (here). They are split so each file stays
readable; the split is along a seam, not an arbitrary line count.

This is a mixin rather than a standalone collaborator because the loop reads
the runtime's client and its configured limits directly. The attributes it
requires are declared below, so the coupling is written down instead of
being discovered at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sciteam.output_truncation import persist_full_text
from sciteam.worker_common import extract_json as _extract_json
from sciteam.worker_common import sanitize_artifact_payload

if TYPE_CHECKING:
    from sciteam.llm import LlmClient
    from sciteam.session import FileSessionStore

logger = logging.getLogger(__name__)


# E6: thin call-site shims that omit `plugins=` entirely when no registry is
# attached, so every existing caller/monkeypatch of the single-argument
# `wizard_tools.tool_permission`/`tool_deterministic`/`run_research_tool`
# (predating the E6 plugin fallback) keeps working byte-for-byte — the new
# keyword only ever appears on the wire when a composition root actually
# attached a `PluginRegistry`.
def _call_tool_permission(fn: Any, name: str, registry: Any) -> str:
    return fn(name, plugins=registry) if registry is not None else fn(name)


def _call_tool_deterministic(fn: Any, name: str, registry: Any) -> bool:
    return fn(name, plugins=registry) if registry is not None else fn(name)


def _call_run_research_tool(
    fn: Any, name: str, args: dict[str, Any], *, context: dict[str, Any], registry: Any
) -> dict[str, Any]:
    if registry is not None:
        return fn(name, args, context=context, plugins=registry)
    return fn(name, args, context=context)


# P1-7: max schema-validator-guided repair round-trips for a contract
# ("artifact") violation before giving up and letting the engine's own
# downstream ContractValidator reject the artifact as before.
_MAX_CONTRACT_REPAIR_ATTEMPTS = 2
_JSON_OBJECT_RESPONSE_FORMAT = {"type": "json_object"}

# How many of the most recent `role: "tool"` messages are always kept
# verbatim when the inner loop's tool history is compacted, so the model can
# still reason about what it just did.
_TOOL_HISTORY_KEEP_RECENT = 3


class ToolLoopMixin:
    """Mixed into :class:`~sciteam.llm_worker.LlmWorkerRuntime`."""

    # Provided by the concrete runtime's __init__ (see llm_worker.py).
    _client: LlmClient
    _max_tokens: int
    _temperature: float
    _max_tool_rounds: int
    _max_parallel_readonly: int
    _tool_history_budget_chars: int
    _tool_result_visible_chars: int
    _tool_result_preview_chars: int
    _schemas_dir: Path | None
    # E4 session kernel (docs/SESSION-KERNEL.md): optional, additive.
    # When set by the composition root, every LLM turn and tool call/result
    # in this inner loop is also recorded as a hash-chained SessionEvent —
    # a *second*, crash-resumable, third-party-verifiable record of what the
    # model and tools actually did, correlated to (but never merged with)
    # the institution ledger. `messages` (the provider-visible context)
    # keeps being built exactly as before this write path was added: full
    # migration to session-log-as-source-of-truth is a follow-up, not this
    # change — see docs/SESSION-KERNEL.md §9 and 28-... §7.2 (E4 scope).
    _session_store: FileSessionStore | None
    # E6: optional installed-plugin registry. `None` (default) means no
    # plugin hooks run — exactly today's behavior for every caller that
    # does not opt in via a composition root (`sciteam/composition.py`).
    _plugin_registry: Any

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

        # E6 (28-... §7.2): the `tool_call` hot path is one of exactly two
        # blockable institutional events (`plugin_api.BLOCKABLE_EVENTS`).
        # Before this call, an installed plugin's hook was only exercised in
        # tests and example scripts — a real inner loop never reached it.
        # `registry.emit` runs every plugin's hook regardless (observation
        # is unconditional); only a `block=True` short-circuits execution,
        # and it does so by returning a normal-shaped `ok: False` tool
        # result, never a raised exception — a plugin denial is an
        # institutional refusal, not a crash.
        registry = getattr(self, "_plugin_registry", None)
        if registry is not None:
            hook_result = registry.emit(
                "tool_call",
                {"tool_name": name, "arguments": dict(args), "tool_context": dict(tool_context)},
            )
            if hook_result.block:
                blocked = {
                    "ok": False,
                    "blocked": True,
                    "reason": hook_result.reason or "blocked by an installed plugin hook",
                    "tool": name,
                }
                return tc, name, args, blocked, False

        # P1-7: reuse a same-work-item (tool, args) result instead of
        # re-invoking — only for catalog `deterministic: true` tools (e.g.
        # literature_search, run_eval), never for web_search/submit_job
        # where a repeat call may legitimately return something different.
        # This is the concrete fix for "thinking loops in place": a model
        # that re-issues an identical query gets the same answer instantly
        # rather than burning a tool round (and, for side-effect-adjacent
        # deterministic tools, re-triggering real work).
        cache_key: tuple[str, str] | None = None
        if cache is not None and _call_tool_deterministic(tool_deterministic, name, registry):
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
                    _call_run_research_tool,
                    run_research_tool,
                    name,
                    args,
                    context=tool_context,
                    registry=registry,
                )
        else:
            result = await asyncio.to_thread(
                _call_run_research_tool,
                run_research_tool,
                name,
                args,
                context=tool_context,
                registry=registry,
            )
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

        registry = getattr(self, "_plugin_registry", None)
        results: list[tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], bool]] = []
        i = 0
        n = len(calls)
        while i < n:
            name0, _ = self._parse_tool_call(calls[i])
            readonly = _call_tool_permission(tool_permission, name0, registry) == "readonly"
            j = i + 1
            while j < n:
                namej, _ = self._parse_tool_call(calls[j])
                perm_j = _call_tool_permission(tool_permission, namej, registry)
                if (perm_j == "readonly") != readonly:
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

    @staticmethod
    def _session_id_for(tool_context: dict[str, Any]) -> str:
        parts = (
            str(tool_context.get("team_run_id") or "run"),
            str(tool_context.get("agent_key") or "agent"),
            str(tool_context.get("work_item_id") or "item"),
        )
        return ":".join(re.sub(r"[^A-Za-z0-9_.-]+", "_", p) for p in parts)

    def _session_log(self, session_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        """Best-effort append to the E4 session log. Never raises into the
        inner loop — a session-logging failure must not take down a work
        item that is otherwise succeeding, same discipline as the existing
        `except OSError: logger.warning(...)` around `append_inner_loop_trace`."""
        store = getattr(self, "_session_store", None)
        if store is None or not session_id:
            return
        try:
            store.append(session_id, kind, payload)
        except OSError:
            logger.warning("session log append failed for %s (continuing)", session_id)

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
        session_id: str | None = None,
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
            # E4/E5 seam: this is stage-1 "mechanical prune" of the three-stage
            # CompactionPort (docs/SESSION-KERNEL.md §5) — no LLM call, no
            # summarization yet. Recording it as a `compaction` SessionEvent
            # now means forgetting is auditable from day one of the session
            # log, even before the full summarizing stage lands.
            self._session_log(
                session_id,
                "compaction",
                {
                    "trigger": "tool_history_budget",
                    "tool_round": tool_round,
                    "messages_compacted": n_compacted,
                    "chars_saved": chars_saved,
                    "tokens_before": None,
                    "tokens_after": None,
                    "pins_preserved": True,
                },
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

        # E4 session kernel: this call happens exactly once per work item
        # (llm_worker.run_subagent's single call site), so a local
        # `session_id` — not instance state — is safe under the same
        # concurrency model P1-7 already relies on for `tool_result_cache`.
        session_id = self._session_id_for(tool_context)
        for msg in messages:
            if msg.get("role") == "user":
                self._session_log(session_id, "user_message", {"content": msg.get("content")})

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
            self._session_log(session_id, "assistant_message", {"content": resp.content or ""})
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
                self._session_log(session_id, "assistant_message", {"content": resp.content or ""})
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
                self._session_log(session_id, "assistant_message", {"content": final.content or ""})
                return final
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or None,
                    "tool_calls": resp.tool_calls,
                }
            )
            self._session_log(
                session_id,
                "assistant_message",
                {"content": resp.content or None, "tool_calls": resp.tool_calls},
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
                self._session_log(
                    session_id, "tool_call", {"call_id": call_id, "tool": name, "arguments": args}
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
                result_ref = archive_path
                store = getattr(self, "_session_store", None)
                if store is not None and session_id:
                    with contextlib.suppress(OSError):
                        result_ref = store.archive(full_text)
                self._session_log(
                    session_id,
                    "tool_result",
                    {
                        "call_id": call_id,
                        "tool": name,
                        "result_ref": result_ref,
                        "result_chars": len(full_text),
                        "truncated": len(full_text) > self._tool_result_visible_chars,
                    },
                )
            self._compact_tool_history(
                messages,
                tool_context=tool_context,
                inner_loop=inner_loop,
                tool_round=tool_round,
                session_id=session_id,
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
        self._session_log(session_id, "assistant_message", {"content": final.content or ""})
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
