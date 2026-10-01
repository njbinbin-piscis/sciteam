"""Single-agent activation loop (docs/20-harness.md §3)."""

import asyncio
import hashlib
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from sciteam.harness.budget import budget_status
from sciteam.harness.compaction import Compactor
from sciteam.harness.config import HarnessConfig
from sciteam.harness.context import assemble, fixed_tokens, harness_contract
from sciteam.harness.dispatch import dispatch
from sciteam.harness.state import ActivationState, Totals, apply
from sciteam.harness.tools import BUILTIN_TOOLS, FINISH_SPEC, Tool, ToolContext, cap_output
from sciteam.harness.tools.base import ToolArgError, validate_args
from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.clock import Clock, SystemClock
from sciteam.kernel.events import EventStore
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.pricing import cost_usd
from sciteam.providers.llm.types import (
    AssistantTurn,
    CallParams,
    ChatMessage,
    ContextOverflow,
    LlmError,
    LlmProvider,
    ModelInfo,
    RateLimited,
    ToolCall,
    ToolSpec,
    Transient,
)
from sciteam.providers.sandbox.base import Sandbox

FinishCheck = Callable[[dict[str, Any], ToolContext], Awaitable[str | None]]
# Returns (text, meta) pairs to append as incoming entries before the next model call.
PollIncoming = Callable[[], Awaitable[list[tuple[str, dict[str, Any]]]]]

HARNESS_OUTCOMES = ("budget_exhausted", "error")

FINISH_REVIEW = """[Harness] Finish review: this finish is not accepted yet. Check the result against the task one more time before finishing.

Re-read the task below. For each explicit requirement (file names and paths, output formats, field and key names, exact values and numbers, size or time limits, what must be installed or running), verify it against the actual current state by running commands or tests now; do not rely on earlier output or memory. Recompute any reported numbers independently. Fix anything that does not match. Then call finish again; the next finish will be accepted.

{task}"""  # noqa: E501


class ActivationResult(BaseModel):
    activation_id: str
    outcome: str
    summary: str = ""
    artifacts: list[str] = []
    claims: list[str] = []
    reason: str | None = None
    totals: Totals
    steps: int
    wall_s: float
    compactions: int


class _Fatal(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _sig(call: ToolCall, content: str) -> str:
    h = hashlib.sha256()
    h.update(call.name.encode())
    h.update(call.arguments_raw.encode())
    h.update(content.encode())
    return h.hexdigest()[:16]


class Harness:
    def __init__(self, cfg: HarnessConfig, *, model_info: ModelInfo, llm: LlmProvider,
                 sandbox: Sandbox, store: EventStore, blobs: BlobStore, system_prompt: str,
                 summarizer: LlmProvider | None = None, clock: Clock | None = None,
                 extra_tools: dict[str, Tool] | None = None, finish_check: FinishCheck | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 tool_extras: dict[str, Any] | None = None,
                 poll_incoming: PollIncoming | None = None):
        self.cfg = cfg
        self.model_info = model_info
        self.llm = llm
        self.sandbox = sandbox
        self.store = store
        self.blobs = blobs
        self.clock = clock or SystemClock()
        self.finish_check = finish_check
        self.on_event = on_event
        self.tool_extras = tool_extras or {}
        self.poll_incoming = poll_incoming
        self.system_prompt = system_prompt + harness_contract(cfg)
        all_tools = {**BUILTIN_TOOLS, **(extra_tools or {})}
        self.tools: dict[str, Tool] = {n: all_tools[n] for n in cfg.tools if n != "finish"}
        self.tool_specs: list[ToolSpec] = [t.spec for t in self.tools.values()] + [FINISH_SPEC]
        self.fixed_tokens = fixed_tokens(self.system_prompt, self.tool_specs)
        self.compactor = Compactor(
            cfg.compaction, window=model_info.context_window, summarizer=summarizer or llm,
            clock=self.clock, cost_fn=lambda u, ts: cost_usd(u, model_info, ts))
        self.state = ActivationState()
        self.stream = ""
        self._t0 = 0.0
        self._recent: deque[str] = deque(maxlen=cfg.recovery.loop_window)
        self._loop_warnings = 0
        self._skip_compaction_until = 0

    # ---- events -------------------------------------------------------------------------
    def _emit(self, type: str, payload: dict[str, Any]) -> None:
        ev = self.store.append(self.stream, type, payload, actor=self.state.activation_id or None)
        apply(self.state, ev, self.blobs)
        if self.on_event:
            self.on_event(type, payload)

    def _notice(self, text: str, key: str | None = None) -> None:
        self._emit("notice_added", {"text": text, "key": key})

    def _wall(self) -> float:
        return self.state.wall_elapsed_before_resume + (self.clock.monotonic() - self._t0)

    def _ctx(self) -> ToolContext:
        remaining = self.cfg.budget.wall_clock_s - self._wall()
        return ToolContext(sandbox=self.sandbox, blobs=self.blobs, state=self.state,
                           config=self.cfg, clock=self.clock, remaining_wall_s=max(remaining, 1),
                           extras=self.tool_extras)

    # ---- model calls --------------------------------------------------------------------
    def _params(self) -> CallParams:
        return CallParams(reasoning_effort=self.cfg.reasoning_effort,
                          max_output_tokens=self.cfg.max_output_tokens,
                          timeout_s=self.cfg.call_timeout_s)

    async def _call(self, msgs: list[ChatMessage], tools: list[ToolSpec], params: CallParams
                    ) -> AssistantTurn:
        rc = self.cfg.recovery
        attempt = 0
        while True:
            try:
                return await self.llm.complete(msgs, tools, params=params)
            except (RateLimited, Transient) as e:
                attempt += 1
                self._emit("recovery", {"category": e.category, "attempt": attempt,
                                        "detail": str(e)[:500]})
                if attempt > rc.provider_retries:
                    raise _Fatal("provider_unavailable", str(e)[:500]) from e
                delay = e.retry_after or min(rc.backoff_base_s * 2 ** (attempt - 1),
                                             rc.backoff_max_s)
                await asyncio.sleep(delay)

    def _record_turn(self, turn: AssistantTurn, duration: float) -> None:
        ref = self.blobs.put_text(turn.model_dump_json()).sha256
        cost = cost_usd(turn.usage, self.model_info, self.clock.now())
        self._emit("model_returned", {
            "turn_ref": ref, "cost_usd": cost, "usage": turn.usage.model_dump(),
            "finish_reason": turn.finish_reason, "tool_names": [c.name for c in turn.tool_calls],
            "content_chars": len(turn.content), "reasoning_chars": len(turn.reasoning or ""),
            "duration_s": round(duration, 3), "model": turn.model,
        })

    # ---- compaction ---------------------------------------------------------------------
    async def _compact(self, reason: str) -> None:
        before = self.state.context_tokens(self.fixed_tokens)
        self._emit("compaction_started", {"reason": reason, "context_tokens": before,
                                          "threshold": self.compactor.threshold})
        tail = None
        for attempt in range(2):
            out = await self.compactor.compact(self.state, tail_tokens=tail)
            for c in out.calls:
                self._emit("summary_usage", {"usage": c.usage.model_dump(), "cost_usd": c.cost_usd,
                                             "ok": c.ok, "error": c.error})
            if out.record is None:
                if reason == "overflow" or attempt:
                    raise _Fatal("compaction_failed", out.reason or "")
                self._emit("compaction_skipped", {"reason": out.reason, "context_tokens": before})
                self._skip_compaction_until = len(self.state.entries) + 2
                return
            rec = out.record
            live = sum(e.tokens for e in self.state.entries[: self.state.pinned])
            live += sum(e.tokens for e in self.state.entries[rec.first_kept:])
            rec.tokens_before = before
            rec.tokens_after = self.fixed_tokens + live + len(rec.summary) // 4
            self._emit("compaction_finished", {"record": rec.model_dump(), "reason": reason})
            if rec.tokens_after <= self.compactor.threshold or reason == "overflow" and attempt:
                return
            tail = self.compactor.tail_tokens() // 2
            before = rec.tokens_after
        if self.state.context_tokens(self.fixed_tokens) > self.compactor.threshold:
            raise _Fatal("compaction_insufficient",
                         f"context still {self.state.context_tokens(self.fixed_tokens)} tokens")

    # ---- budget -------------------------------------------------------------------------
    def _budget_notices(self) -> None:
        b = self.cfg.budget
        st = budget_status(b, self.state, self._wall())
        sent = self.state.notices_sent
        if st.fraction >= b.decide_at and "budget_decide" not in sent:
            self._notice(f"[Harness] {int(b.decide_at * 100)}% of the {st.binding} limit is used "
                         f"({st.line(b)}). Decide now: either state the concrete remaining steps "
                         f"and continue, or call finish (use outcome=handoff if work remains).",
                         key="budget_decide")
        elif st.fraction >= b.warn_at and "budget_warn" not in sent:
            self._notice(f"[Harness] {int(b.warn_at * 100)}% of the {st.binding} limit is used "
                         f"({st.line(b)}). Plan to wrap up: prioritise verifying your work.",
                         key="budget_warn")
        elif self.state.steps and self.state.steps % b.status_every_steps == 0 \
                and f"status_{self.state.steps}" not in sent:
            self._notice(f"[Harness] {st.line(b)}.", key=f"status_{self.state.steps}")

    async def _closing_call(self, binding: str) -> None:
        self._notice(f"[Harness] The {binding} limit is exhausted. Do not start new work. Call "
                     f"finish now with outcome=handoff and a summary of what was done, what "
                     f"remains, and the exact state of the workspace.", key="budget_exhausted")
        params = self._params()
        if self.model_info.forced_tool_choice:
            params.tool_choice = {"type": "function", "function": {"name": "finish"}}
        summary, artifacts, claims = "", [], []
        try:
            t0 = self.clock.monotonic()
            turn = await self._call(assemble(self.system_prompt, self.state), [FINISH_SPEC],
                                    params)
            self._record_turn(turn, self.clock.monotonic() - t0)
            fin = next((c for c in turn.tool_calls if c.name == "finish"), None)
            if fin and fin.arguments:
                summary = str(fin.arguments.get("summary", ""))
                artifacts = list(fin.arguments.get("artifacts") or [])
                claims = list(fin.arguments.get("claims") or [])
            else:
                summary = turn.content
            for c in turn.tool_calls:
                self._tool_finished(c, "[activation ended: budget exhausted]", None, False, {}, 0)
        except (_Fatal, LlmError) as e:
            summary = f"[closing call failed: {e}]"
        self._finish("budget_exhausted", summary, artifacts, claims, reason=binding)

    # ---- tools --------------------------------------------------------------------------
    def _tool_finished(self, call: ToolCall, content: str, full_ref: str | None, is_error: bool,
                       meta: dict, duration: float) -> None:
        self._emit("tool_finished", {
            "call_id": call.id, "name": call.name, "content_ref": self.blobs.put_text(content).sha256,
            "full_output_ref": full_ref, "is_error": is_error, "meta": meta,
            "duration_s": round(duration, 3), "chars": len(content),
        })

    async def _handle_calls(self, calls: list[ToolCall]) -> None:
        finish_calls = [c for c in calls if c.name == "finish"]
        others = [c for c in calls if c.name != "finish"]
        to = self.cfg.tool_output
        ctx = self._ctx()
        results = await dispatch(
            others, self.tools, ctx, parallel_readonly=to.parallel_readonly,
            on_start=lambda c: self._emit("tool_started", {"call_id": c.id, "name": c.name}))
        for r in results:
            content, ref = cap_output(r.result.content, max_chars=to.max_chars,
                                      head_fraction=to.head_fraction, blobs=self.blobs)
            self._tool_finished(r.call, content, ref, r.result.is_error, r.result.meta,
                                r.duration_s)
            self._recent.append(_sig(r.call, r.result.content))
        if not finish_calls:
            self._check_loop()
            return
        fin, dups = finish_calls[0], finish_calls[1:]
        for d in dups:
            self._tool_finished(d, "error: only one finish call is allowed per turn", None, True,
                                {}, 0)
        err = self._validate_finish(fin)
        if err is None and any(r.result.is_error for r in results):
            err = ("other tool calls in the same turn failed; review their results before "
                   "finishing")
        if err is None and self.finish_check:
            err = await self.finish_check(fin.arguments or {}, self._ctx())
        if err is not None:
            self._tool_finished(fin, f"error: finish rejected: {err}", None, True, {}, 0)
            return
        if self._wants_review(fin):
            self._tool_finished(fin, FINISH_REVIEW.format(task=self.state.entries[0].msg.content),
                                None, False, {"finish_review": True}, 0)
            return
        self._tool_finished(fin, "activation finished", None, False, {}, 0)
        a = fin.arguments or {}
        self._finish(a["outcome"], str(a.get("summary", "")), list(a.get("artifacts") or []),
                     list(a.get("claims") or []))

    def _wants_review(self, fin: ToolCall) -> bool:
        if (fin.arguments or {}).get("outcome") != "completed":
            return False
        if self.state.finish_reviews >= self.cfg.finish_reviews:
            return False
        # Near the budget there is no room for another pass; accept rather than risk the limit.
        return budget_status(self.cfg.budget, self.state, self._wall()).fraction < \
            self.cfg.budget.decide_at

    def _validate_finish(self, fin: ToolCall) -> str | None:
        if fin.arguments is None:
            return f"arguments are not valid JSON: {fin.arguments_raw[:300]!r}"
        try:
            validate_args(FINISH_SPEC, fin.arguments)
        except ToolArgError as e:
            return str(e)
        return None

    def _check_loop(self) -> None:
        rc = self.cfg.recovery
        if not self._recent:
            return
        last = self._recent[-1]
        if sum(1 for s in self._recent if s == last) < rc.loop_repeat:
            return
        self._loop_warnings += 1
        self._recent.clear()
        if self._loop_warnings == 1:
            self._notice("[Harness] You have repeated the same tool call with the same result "
                         f"{rc.loop_repeat} times. Change approach: re-read the relevant code or "
                         "error, form a different hypothesis, or try a different tool.",
                         key="loop_1")
        else:
            self._notice("[Harness] You are repeating identical tool calls again. Decide now: "
                         "state a concretely different plan and follow it, or call finish "
                         "(outcome=blocked or handoff) explaining what blocks you.",
                         key=f"loop_{self._loop_warnings}")

    # ---- finish -------------------------------------------------------------------------
    def _finish(self, outcome: str, summary: str, artifacts: list[str], claims: list[str],
                reason: str | None = None) -> None:
        self._emit("activation_finished", {
            "outcome": outcome, "summary": summary, "artifacts": artifacts, "claims": claims,
            "reason": reason, "totals": self.state.totals.model_dump(), "steps": self.state.steps,
            "wall_s": round(self._wall(), 3), "compactions": len(self.state.compactions),
        })

    def _result(self) -> ActivationResult:
        f = self.state.finished or {}
        return ActivationResult(
            activation_id=self.state.activation_id, outcome=f.get("outcome", "error"),
            summary=f.get("summary", ""), artifacts=f.get("artifacts", []),
            claims=f.get("claims", []), reason=f.get("reason"), totals=self.state.totals,
            steps=self.state.steps, wall_s=f.get("wall_s", self._wall()),
            compactions=len(self.state.compactions))

    # ---- entry points -------------------------------------------------------------------
    async def run(self, task: str, *, stream: str | None = None, meta: dict | None = None,
                  activation_id: str | None = None) -> ActivationResult:
        activation_id = activation_id or new_id("act")
        self.stream = stream or f"act/{activation_id}"
        self._t0 = self.clock.monotonic()
        self._emit("activation_started", {
            "activation_id": activation_id, "task_ref": self.blobs.put_text(task).sha256,
            "harness": self.cfg.model_dump(mode="json"), "model": self.model_info.name,
            "system_prompt_ref": self.blobs.put_text(self.system_prompt).sha256,
            "tools": [t.name for t in self.tool_specs], "meta": meta or {},
        })
        return await self._drive()

    async def resume(self, stream: str) -> ActivationResult:
        """Continue an interrupted activation from its event stream (docs/20-harness.md §3)."""
        self.stream = stream
        events = self.store.read(stream)
        if not events:
            raise ValueError(f"no events in {stream}")
        for ev in events:
            apply(self.state, ev, self.blobs)
        self.state.wall_elapsed_before_resume = (events[-1].ts - events[0].ts).total_seconds()
        self._t0 = self.clock.monotonic()
        if self.state.finished:
            return self._result()
        self._emit("activation_resumed", {"after_stream_seq": events[-1].stream_seq})
        for c in list(self.state.pending_tool_calls):
            self._tool_finished(c, "[result unknown: the harness restarted while this tool call "
                                   "was in flight. Re-check the workspace state before relying "
                                   "on its effects.]", None, True, {"resumed": True}, 0)
        return await self._drive()

    async def _drive(self) -> ActivationResult:
        rc = self.cfg.recovery
        no_tool_turns = 0
        length_turns = 0
        overflow_retries = 0
        try:
            while self.state.finished is None:
                st = budget_status(self.cfg.budget, self.state, self._wall())
                if st.fraction >= 1.0:
                    await self._closing_call(st.binding)
                    break
                self._budget_notices()
                if self.poll_incoming is not None:
                    for text, meta in await self.poll_incoming():
                        self._emit("incoming_added", {"text": text, "meta": meta})
                if self.compactor.should_compact(self.state.context_tokens(self.fixed_tokens)) \
                        and len(self.state.entries) >= self._skip_compaction_until:
                    await self._compact("threshold")
                msgs = assemble(self.system_prompt, self.state)
                self._emit("model_called", {
                    "n_messages": len(msgs), "first_kept": self.state.first_kept,
                    "compaction": len(self.state.compactions),
                    "context_tokens_est": self.state.context_tokens(self.fixed_tokens)})
                t0 = self.clock.monotonic()
                try:
                    turn = await self._call(msgs, self.tool_specs, self._params())
                except ContextOverflow as e:
                    overflow_retries += 1
                    self._emit("recovery", {"category": "context_overflow",
                                            "attempt": overflow_retries, "detail": str(e)[:300]})
                    if overflow_retries > 2:
                        raise _Fatal("context_overflow", str(e)[:500]) from e
                    await self._compact("overflow")
                    continue
                except LlmError as e:
                    raise _Fatal(e.category, str(e)[:1000]) from e
                overflow_retries = 0
                self._record_turn(turn, self.clock.monotonic() - t0)
                if turn.tool_calls:
                    no_tool_turns = 0
                    length_turns = 0
                    await self._handle_calls(turn.tool_calls)
                    continue
                if turn.finish_reason == "length":
                    length_turns += 1
                    if length_turns > rc.length_retries:
                        raise _Fatal("output_length", "repeated max-output truncation")
                    self._notice("[Harness] Your reply hit the output token limit before any tool "
                                 "call. Work in smaller steps and call a tool.")
                    continue
                no_tool_turns += 1
                if no_tool_turns > rc.empty_turn_retries:
                    raise _Fatal("no_finish", turn.content[-2000:])
                self._notice("[Harness] You replied without calling a tool. Continue the work "
                             "with tool calls, or call finish to end this activation.")
        except _Fatal as e:
            if self.state.finished is None:
                self._finish("error", e.detail, [], [], reason=e.reason)
        except asyncio.CancelledError:
            if self.state.finished is None:
                self._finish("error", "", [], [], reason="cancelled")
            raise
        except Exception as e:
            if self.state.finished is None:
                self._finish("error", f"{type(e).__name__}: {e}", [], [], reason="harness_bug")
            raise
        return self._result()
