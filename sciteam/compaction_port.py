"""``CompactionPort`` — docs/SESSION-KERNEL.md §5, and overflow recovery §6.

Three mechanical stages, run in order:

1. Prune — already happens at write time in this codebase: `tool_loop.py`
   archives full tool results and keeps only a `result_ref` in the session
   log's `tool_result` payload (see `docs/SESSION-KERNEL.md` §1's `kind`
   table). There is nothing left to prune post-hoc at the session-event
   level; this module's "prune" stage is choosing *how far back* the
   projection window reaches, which `project()` already implements via
   `latest_compaction_seq`.
2. Summarize — one call through an injected `Summarizer` port (never a
   hardcoded HTTP call in this module — same Provider-injection discipline
   the rest of the package follows).
3. Reject if non-shrinking — a summary that does not actually shrink the
   window is discarded outright; the caller is not allowed to loop on a
   broken summarizer forever (`compaction_retries`, default 1).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sciteam.session import FileSessionStore, SessionEvent
from sciteam.token_meter import TokenReading, estimate_tokens_from_chars

logger = logging.getLogger(__name__)

DEFAULT_PRESSURE_THRESHOLD = 0.8
DEFAULT_COMPACTION_RETRIES = 1


class Summarizer(Protocol):
    """Injected summarization port — a Provider, not a hardcoded client."""

    async def summarize(self, text: str, *, budget_tokens: int) -> str: ...


@dataclass(frozen=True, slots=True)
class CompactionResult:
    summary: str
    summary_ref: str
    from_seq: int
    to_seq: int
    tokens_before: int
    tokens_after: int
    pins_preserved: bool = True


class CompactionRejected(Exception):
    """A compaction attempt did not shrink the window and was discarded."""


def _renderable_text(event: SessionEvent) -> str:
    payload = event.payload
    if event.kind == "user_message":
        return str(payload.get("content") or "")
    if event.kind == "assistant_message":
        return str(payload.get("content") or "")
    if event.kind == "tool_call":
        return f"[called {payload.get('tool')} with {payload.get('arguments')}]"
    if event.kind == "tool_result":
        return f"[result of {payload.get('tool')}: ref={payload.get('result_ref')}]"
    return ""


class MechanicalCompactionPort:
    """Default `CompactionPort` implementation.

    ``pins`` (docs/SESSION-KERNEL.md §3.1 — constitution, seat authority,
    rule version, open vetoes/obligations, cited evidence pointers) are
    passed through opaquely and are never included in the text handed to
    the summarizer or in the compacted window; they are rendered by
    `sciteam.session.project` as an unconditional prefix, entirely outside
    this port's authority. This port has no parameter through which a pin
    could be folded, elided, or summarized — by construction, not by
    convention.
    """

    def __init__(
        self,
        *,
        summarizer: Summarizer | None = None,
        pressure_threshold: float = DEFAULT_PRESSURE_THRESHOLD,
    ) -> None:
        self._summarizer = summarizer
        self._pressure_threshold = pressure_threshold

    def should_compact(self, reading: TokenReading, window: int) -> bool:
        if window <= 0:
            raise ValueError("window must be positive")
        return (reading.total_tokens / window) >= self._pressure_threshold

    async def compact(
        self,
        events: Sequence[SessionEvent],
        *,
        pins: Sequence[str],
        keep_recent_tokens: int,
        session_store: FileSessionStore | None = None,
    ) -> CompactionResult | None:
        """Fold ``events`` older than the most recent ``keep_recent_tokens``
        worth of content into one summary. Returns ``None`` when there is no
        safe range to fold (too little history) — callers must not treat
        that as an error and must not retry-loop on it.
        """
        foldable = [e for e in events if e.kind not in ("checkpoint",)]
        if len(foldable) < 2:
            return None

        # Walk backwards accumulating estimated tokens until the "keep
        # recent" budget is exhausted; everything before that point folds.
        kept_tokens = 0
        cutoff_index = len(foldable)
        for index in range(len(foldable) - 1, -1, -1):
            text = _renderable_text(foldable[index])
            kept_tokens += estimate_tokens_from_chars(text)
            if kept_tokens >= keep_recent_tokens:
                cutoff_index = index
                break
        else:
            return None  # everything fits in the recent-keep budget already

        foldable_range = foldable[:cutoff_index]
        if len(foldable_range) < 2:
            return None

        rendered = (_renderable_text(e) for e in foldable_range)
        original_text = "\n".join(text for text in rendered if text)
        tokens_before = estimate_tokens_from_chars(original_text)
        if tokens_before == 0:
            return None

        if self._summarizer is not None:
            summary = await self._summarizer.summarize(
                original_text, budget_tokens=max(64, keep_recent_tokens // 4)
            )
        else:
            # No summarizer injected: fall back to a deterministic,
            # model-free digest so this port is usable (and testable)
            # without a live LLM — never silently no-op.
            summary = (
                f"[{len(foldable_range)} earlier turns folded, "
                f"~{tokens_before} tokens; head: {original_text[:200]!r}]"
            )

        tokens_after = estimate_tokens_from_chars(summary)
        if tokens_after >= tokens_before:
            # Non-shrinking guard (docs/SESSION-KERNEL.md §5): reject
            # outright rather than accept a "compaction" that made things
            # worse or even just no better.
            raise CompactionRejected(
                f"summary ({tokens_after} tok) did not shrink source ({tokens_before} tok)"
            )

        summary_ref = ""
        if session_store is not None:
            summary_ref = session_store.archive(original_text)

        return CompactionResult(
            summary=summary,
            summary_ref=summary_ref,
            from_seq=foldable_range[0].seq,
            to_seq=foldable_range[-1].seq,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            pins_preserved=True,
        )


async def compact_with_retry_budget(
    port: MechanicalCompactionPort,
    events: Sequence[SessionEvent],
    *,
    pins: Sequence[str],
    keep_recent_tokens: int,
    session_store: FileSessionStore | None = None,
    max_retries: int = DEFAULT_COMPACTION_RETRIES,
) -> CompactionResult | None:
    """Bounded retry wrapper (docs/SESSION-KERNEL.md §5 point 3): a rejected
    (non-shrinking) compaction is retried at most ``max_retries`` times with
    a strictly larger keep-budget floor, never in an unbounded loop."""
    floor = keep_recent_tokens
    for attempt in range(max_retries + 1):
        try:
            return await port.compact(
                events, pins=pins, keep_recent_tokens=floor, session_store=session_store
            )
        except CompactionRejected as exc:
            logger.warning("compaction attempt %d rejected: %s", attempt + 1, exc)
            floor = int(floor * 1.5) + 1
    return None
