import asyncio
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel

from sciteam.harness.compaction.cut import find_cut_point, tail_budget
from sciteam.harness.compaction.extract import (
    build_anchor_index,
    build_verbatim_user_section,
    extract_file_ops,
    fallback_summary,
    format_file_ops,
    recovery_footer,
    serialize_conversation,
)
from sciteam.harness.compaction.prompts import (
    SESSION_LOG_SECTION,
    SUMMARIZATION_PROMPT,
    SUMMARIZATION_SYSTEM_PROMPT,
    TURN_PREFIX_SUMMARIZATION_PROMPT,
    UPDATE_SUMMARIZATION_PROMPT,
)
from sciteam.harness.config import CompactionConfig
from sciteam.harness.state import ActivationState, CompactionRecord, Entry
from sciteam.kernel.clock import Clock
from sciteam.providers.llm.types import (
    CallParams,
    ChatMessage,
    LlmError,
    LlmProvider,
    RateLimited,
    Transient,
    Usage,
)


class SummaryCall(BaseModel):
    usage: Usage
    cost_usd: float
    ok: bool
    error: str | None = None


class CompactionOutcome(BaseModel):
    record: CompactionRecord | None
    calls: list[SummaryCall]
    reason: str | None = None  # why nothing was compacted


class Compactor:
    def __init__(self, cfg: CompactionConfig, *, window: int, summarizer: LlmProvider,
                 clock: Clock, cost_fn: Callable[[Usage, datetime], float]):
        self.cfg = cfg
        self.window = window
        self.summarizer = summarizer
        self.clock = clock
        self.cost_fn = cost_fn
        self.cooldown_until = 0.0
        self.consecutive_failures = 0

    @property
    def threshold(self) -> int:
        return min(int(self.cfg.trigger_fraction * self.window), self.cfg.trigger_abs_cap)

    def should_compact(self, context_tokens: int) -> bool:
        return self.cfg.enabled and context_tokens > self.threshold

    def tail_tokens(self) -> int:
        c = self.cfg
        return tail_budget(self.window, fraction=c.tail_fraction, floor=c.tail_min, cap=c.tail_max,
                           max_window_fraction=c.tail_max_window_fraction)

    async def _summarize(self, prompt_text: str, max_out: int, calls: list[SummaryCall]) -> str:
        msgs = [ChatMessage(role="system", content=SUMMARIZATION_SYSTEM_PROMPT),
                ChatMessage(role="user", content=prompt_text)]
        params = CallParams(reasoning_effort=self.cfg.summary_reasoning_effort,
                            max_output_tokens=max_out, timeout_s=1800)
        attempt = 0
        while True:
            try:
                turn = await self.summarizer.complete(msgs, [], params=params)
                break
            except LlmError as e:
                calls.append(SummaryCall(usage=Usage(), cost_usd=0.0, ok=False,
                                         error=f"{e.category}: {str(e)[:300]}"))
                attempt += 1
                if not isinstance(e, RateLimited | Transient) or attempt >= 3:
                    raise
                await asyncio.sleep(min(2 ** attempt, 30))
        cost = self.cost_fn(turn.usage, self.clock.now())
        text = turn.content.strip()
        calls.append(SummaryCall(usage=turn.usage, cost_usd=cost, ok=bool(text),
                                 error=None if text else "empty summary"))
        if not text:
            raise LlmError("empty summary")
        return text

    def _clip_region_text(self, text: str) -> str:
        # Keep the summarizer request well inside its window (4 chars ≈ 1 token).
        limit = int(self.window * 0.6) * 4
        if len(text) <= limit:
            return text
        head = int(limit * 0.3)
        return (text[:head] + f"\n\n[... {len(text) - limit} characters of the middle omitted ...]"
                "\n\n" + text[-(limit - head):])

    async def _llm_narrative(self, history: list[Entry], prefix: list[Entry],
                             previous: str | None, calls: list[SummaryCall]) -> str:
        c = self.cfg

        def ser(es: list[Entry]) -> str:
            return self._clip_region_text(serialize_conversation(
                es, tool_chars=c.serialize_tool_result_chars,
                thinking_chars=c.serialize_thinking_chars))

        history_text = previous or "No prior history."
        if history:
            prompt = f"<conversation>\n{ser(history)}\n</conversation>\n\n"
            if previous:
                prompt += f"<previous-summary>\n{previous}\n</previous-summary>\n\n"
            prompt += (UPDATE_SUMMARIZATION_PROMPT if previous else SUMMARIZATION_PROMPT)
            prompt += SESSION_LOG_SECTION
            history_text = await self._summarize(prompt, c.summary_max_output, calls)
        if not prefix:
            return history_text
        prompt = (f"<conversation>\n{ser(prefix)}\n</conversation>\n\n"
                  f"{TURN_PREFIX_SUMMARIZATION_PROMPT}")
        prefix_text = await self._summarize(prompt, c.summary_max_output // 2, calls)
        return f"{history_text}\n\n---\n\n**Turn Context (split turn):**\n\n{prefix_text}"

    async def compact(self, state: ActivationState, *, tail_tokens: int | None = None
                      ) -> CompactionOutcome:
        entries = state.entries
        start, end = state.first_kept, len(entries)
        keep = tail_tokens if tail_tokens is not None else self.tail_tokens()
        cut = find_cut_point(entries, start, end, keep)
        if cut.first_kept <= start:
            return CompactionOutcome(record=None, calls=[], reason="no valid cut point")
        history_end = cut.turn_start if cut.is_split_turn else cut.first_kept
        history = entries[start:history_end]
        prefix = entries[cut.turn_start:cut.first_kept] if cut.is_split_turn else []

        prev = state.summary
        read = set(prev.read_files) if prev else set()
        modified = set(prev.modified_files) if prev else set()
        extract_file_ops(entries[start:cut.first_kept], read, modified)

        calls: list[SummaryCall] = []
        method = "llm"
        now = self.clock.monotonic()
        narrative: str | None = None
        if now >= self.cooldown_until:
            try:
                narrative = await self._llm_narrative(history, prefix,
                                                      prev.narrative if prev else None, calls)
                self.consecutive_failures = 0
            except LlmError:
                self.consecutive_failures += 1
                ladder = self.cfg.cooldown_s
                self.cooldown_until = now + ladder[min(self.consecutive_failures, len(ladder)) - 1]
        if narrative is None:
            method = "fallback"
            narrative = fallback_summary(entries[start:cut.first_kept],
                                         prev.narrative if prev else None)

        folded_all = entries[state.pinned:cut.first_kept]
        c = self.cfg
        full = (narrative + format_file_ops(read, modified)
                + build_anchor_index(folded_all, budget_chars=c.anchor_budget_chars)
                + build_verbatim_user_section(folded_all, budget_chars=c.verbatim_user_budget_chars,
                                              max_chars=c.verbatim_user_message_max_chars)
                + recovery_footer(state.pinned, cut.first_kept))
        record = CompactionRecord(
            n=len(state.compactions) + 1, first_kept=cut.first_kept, folded_from=start,
            folded_to=cut.first_kept, summary=full, narrative=narrative,
            read_files=sorted(read), modified_files=sorted(modified), method=method,
            tokens_before=0, tokens_after=0,
        )
        return CompactionOutcome(record=record, calls=calls)
