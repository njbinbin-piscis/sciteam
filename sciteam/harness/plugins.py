"""DeepSeek Harness Plugin & Lifecycle Hook Chain (`sciteam/harness/plugins.py`).

Integrates:
  1. `HarnessPro` (arXiv:2610.26770 Appendix D.1) runtime hooks:
     - `_pre_tool_use`: enforces per-profile `finalize_after_tools` budget, blocking non-`finish`
       calls once the tool budget is exhausted while preserving current workspace files.
     - `_stage_context` (`post_tool_use`): appends `[Economy phase]` and `[Convergence phase]`
       reminders directly to tool outputs so the LLM prompt prefix (`system_prompt` + prior turns)
       remains 100% byte-identical for KV-cache reuse.
     - `rtk_compact_text`: RTK-style log deduplication that collapses consecutive identical lines
       (`[previous line repeated N more times]`) and bounds head/tail excerpts.
  2. `harness2` `SurfaceSpec.hook_events` (`PreToolUse`, `PostToolUse`, `PreCompact`, `PreFinish`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from sciteam.harness.config import ExecutionProfilePolicy, RuntimePolicyConfig
from sciteam.harness.state import ActivationState
from sciteam.harness.tools.base import ToolResult
from sciteam.providers.llm.types import ToolCall

_LINE_PREFIX_RE = re.compile(r"^\s*\d+[|\t](.*)$")

ECONOMY_PHASE_REMINDER = (
    "[Economy phase starts now. Batch independent ls/rg/read/test checks, suppress "
    "process narration, and do not repeat unchanged checks. Do not trade away the "
    "behavioral validation or exact final-state checks required by the task.]"
)

CONVERGENCE_PHASE_REMINDER = (
    "[Convergence phase: stop broad exploration. Only inspect evidence directly tied to the "
    "current failure or deliverable. If a concrete failure, unexplained instability, or "
    "mutated deliverable remains, repair it rather than finalizing because the phase changed. "
    "Avoid irreversible cleanup or broad state changes not required by the task, and keep "
    "temporary verification artifacts separate from final deliverables. "
    "Then recheck exact paths, interfaces, behavior, and final state once and finish.]"
)

FINALIZE_DENIAL_REASON = (
    "The task tool budget is exhausted. Preserve the current files, "
    "stop calling tools, and give the best final answer from existing evidence."
)


def rtk_compact_text(
    text: str,
    *,
    head_chars: int = 2400,
    tail_chars: int = 900,
    dedup_lines: bool = True,
) -> str:
    """Collapse consecutive duplicate lines (`[previous line repeated N more times]`)
    and bound head/tail characters (HarnessPro Appendix D.1 `_compact_text`)."""
    if not text:
        return ""
    if dedup_lines:
        # Also collapse escaped newline sequences (`\n`) inside JSON-encoded string values if repeated
        working = text.replace("\\n", "\n") if "\\n" in text and "\n" not in text else text
        lines = working.splitlines()
        deduped: list[str] = []
        index = 0
        n_lines = len(lines)

        def _norm_line(ln: str) -> str:
            m = _LINE_PREFIX_RE.match(ln)
            return m.group(1) if m else ln

        while index < n_lines:
            next_index = index + 1
            base_norm = _norm_line(lines[index])
            while next_index < n_lines and _norm_line(lines[next_index]) == base_norm:
                next_index += 1
            repeats = next_index - index
            deduped.append(lines[index])
            if repeats > 2:
                deduped.append(f"[previous line repeated {repeats - 1} more times]")
            elif repeats == 2:
                deduped.append(lines[index + 1])
            index = next_index
        compact = "\n".join(deduped)
    else:
        compact = text

    if head_chars <= 0 and tail_chars <= 0:
        return compact
    if len(compact) <= head_chars + tail_chars:
        return compact
    omitted = len(compact) - head_chars - tail_chars
    return (
        compact[:head_chars]
        + f"\n... [{omitted} characters omitted; use the raw path for a targeted range] ...\n"
        + (compact[-tail_chars:] if tail_chars > 0 else "")
    )


def stage_context_reminder(tool_count: int, profile: ExecutionProfilePolicy) -> str:
    """Return the Economy or Convergence phase reminder for `tool_count` (or empty string)."""
    economy = int(profile.economy_after_tools or 0)
    strict = int(profile.strict_after_tools or 0)
    reminder = max(1, int(profile.strict_reminder_every or 8))

    if economy > 0 and tool_count == economy:
        return ECONOMY_PHASE_REMINDER
    if strict > 0 and tool_count >= strict and (tool_count == strict or (tool_count - strict) % reminder == 0):
        return CONVERGENCE_PHASE_REMINDER
    return ""


@dataclass
class PreToolDecision:
    allow: bool = True
    denial_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class HarnessPlugin(Protocol):
    """Plugin hook interface for DeepSeek Harness lifecycle events."""

    name: str

    def pre_tool_use(
        self,
        call: ToolCall,
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> PreToolDecision: ...

    def post_tool_use(
        self,
        call: ToolCall,
        result: ToolResult,
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> ToolResult: ...

    def pre_finish(
        self,
        args: dict[str, Any],
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> str | None: ...


class BudgetPhaseHookPlugin:
    """Enforces `HarnessPro` tool-call budgets, prefix-preserving stage reminders,
    authoritative real-clock injection, and RTK log dedup."""

    name = "harnesspro_budget_phase_hook"

    def __init__(self, *, wall_budget_s: float = 0.0, clock_notice_every: int = 5) -> None:
        self.tool_count: int = 0
        self.denied_calls: int = 0
        self.economy_reminders_sent: int = 0
        self.convergence_reminders_sent: int = 0
        self.clock_notices_sent: int = 0
        self.rtk_chars_saved: int = 0
        self.wall_budget_s: float = float(wall_budget_s or 0.0)
        self.clock_notice_every: int = max(1, int(clock_notice_every or 5))
        self._t0: float = time.monotonic()
        self._warned_80: bool = False

    def pre_tool_use(
        self,
        call: ToolCall,
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> PreToolDecision:
        if call.name == "finish" or policy is None:
            return PreToolDecision(allow=True)
        self.tool_count += 1
        prof = policy.current_profile()
        finalize = int(prof.finalize_after_tools or 0)
        if finalize > 0 and self.tool_count > finalize:
            self.denied_calls += 1
            return PreToolDecision(
                allow=False,
                denial_reason=FINALIZE_DENIAL_REASON,
                meta={
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "tool_count": self.tool_count,
                    "finalize_after_tools": finalize,
                },
            )
        return PreToolDecision(
            allow=True,
            meta={"tool_count": self.tool_count},
        )

    def post_tool_use(
        self,
        call: ToolCall,
        result: ToolResult,
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> ToolResult:
        if policy is None:
            return result
        content = result.content or ""
        meta = dict(result.meta or {})

        # 1. RTK consecutive duplicate line deduplication
        if policy.tool_output.rtk_enabled and content:
            orig_len = len(content)
            deduped = rtk_compact_text(
                content,
                head_chars=policy.tool_output.head_chars,
                tail_chars=policy.tool_output.tail_chars,
                dedup_lines=True,
            )
            if len(deduped) < orig_len:
                saved = orig_len - len(deduped)
                self.rtk_chars_saved += saved
                meta["rtk_chars_saved"] = saved
                content = deduped

        # 2. Prefix-preserving stage context reminder (appended to tool output)
        prof = policy.current_profile()
        reminder = stage_context_reminder(self.tool_count, prof)
        if reminder:
            if reminder == ECONOMY_PHASE_REMINDER:
                self.economy_reminders_sent += 1
                meta["phase_reminder"] = "economy"
            else:
                self.convergence_reminders_sent += 1
                meta["phase_reminder"] = "convergence"
            content = f"{content}\n\n{reminder}" if content else reminder

        # 3. Authoritative real-clock injection (v3/v5 benchmark-proven discipline):
        # append elapsed/remaining wall-clock time to tool outputs every N steps and
        # once at >=80% budget so the agent never guesses time limits and persists
        # deliverables before cutoff, while keeping the prompt prefix 100% cache-stable.
        if self.wall_budget_s > 0:
            elapsed = max(0.0, time.monotonic() - self._t0)
            remain = max(0.0, self.wall_budget_s - elapsed)
            near_end = remain <= 0.2 * self.wall_budget_s and not self._warned_80
            if (self.tool_count > 0 and self.tool_count % self.clock_notice_every == 0) or near_end:
                clock_msg = (
                    f"[time] elapsed {elapsed:.0f}s of ~{self.wall_budget_s:.0f}s budget; "
                    f"~{remain:.0f}s remain. Persist computed results to workspace deliverables "
                    f"now and call finish as soon as verified."
                )
                self.clock_notices_sent += 1
                meta["clock_notice"] = {"elapsed_s": round(elapsed, 1), "remain_s": round(remain, 1)}
                if near_end:
                    self._warned_80 = True
                content = f"{content}\n{clock_msg}" if content else clock_msg

        return ToolResult(content=content, is_error=result.is_error, meta=meta)

    def pre_finish(
        self,
        args: dict[str, Any],
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> str | None:
        return None


class HarnessPluginChain:
    """Executes an ordered sequence of `HarnessPlugin` instances around tool calls and finish."""

    def __init__(self, plugins: list[Any] | None = None) -> None:
        self.plugins: list[Any] = list(plugins) if plugins is not None else [BudgetPhaseHookPlugin()]

    def run_pre_tool_use(
        self,
        call: ToolCall,
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> PreToolDecision:
        merged_meta: dict[str, Any] = {}
        for p in self.plugins:
            dec = p.pre_tool_use(call, state, policy)
            merged_meta.update(dec.meta)
            if not dec.allow:
                return PreToolDecision(allow=False, denial_reason=dec.denial_reason, meta=merged_meta)
        return PreToolDecision(allow=True, meta=merged_meta)

    def run_post_tool_use(
        self,
        call: ToolCall,
        result: ToolResult,
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> ToolResult:
        cur = result
        for p in self.plugins:
            cur = p.post_tool_use(call, cur, state, policy)
        return cur

    def run_pre_finish(
        self,
        args: dict[str, Any],
        state: ActivationState,
        policy: RuntimePolicyConfig | None,
    ) -> str | None:
        for p in self.plugins:
            err = p.pre_finish(args, state, policy)
            if err:
                return err
        return None
