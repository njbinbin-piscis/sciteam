"""Contract tests for token_meter.py, compaction_port.py, overflow.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.compaction_port import (
    CompactionRejected,
    MechanicalCompactionPort,
    compact_with_retry_budget,
)
from sciteam.overflow import OverflowRecoveryPolicy, classify_error
from sciteam.session import FileSessionStore, project
from sciteam.token_meter import SimpleTokenMeter, estimate_tokens_from_char_count

# ------------------------------------------------------------- TokenMeter


def test_estimate_tokens_heuristic_is_conservative():
    assert estimate_tokens_from_char_count(0) == 0
    assert estimate_tokens_from_char_count(4) == 1
    assert estimate_tokens_from_char_count(3) == 0


def test_measure_marks_estimated_true():
    meter = SimpleTokenMeter()
    ctx = project([], pins=["x" * 400])
    reading = meter.measure(ctx)
    assert reading.estimated is True
    assert reading.source == "heuristic_chars_per_token"
    assert reading.prompt_tokens == 100


def test_from_provider_usage_marks_estimated_false():
    reading = SimpleTokenMeter.from_provider_usage(
        prompt_tokens=120, completion_tokens=30, cached_tokens=10
    )
    assert reading.estimated is False
    assert reading.source == "provider"
    assert reading.total_tokens == 150


def test_pressure_ratio():
    meter = SimpleTokenMeter()
    reading = SimpleTokenMeter.from_provider_usage(prompt_tokens=800, completion_tokens=200)
    assert meter.pressure(reading, window=1000) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        meter.pressure(reading, window=0)


def test_record_call_accumulates_history():
    meter = SimpleTokenMeter()
    r1 = SimpleTokenMeter.from_provider_usage(prompt_tokens=10, completion_tokens=1)
    r2 = SimpleTokenMeter.from_provider_usage(prompt_tokens=20, completion_tokens=2)
    meter.record_call(r1)
    meter.record_call(r2)
    assert meter.history == (r1, r2)


# ---------------------------------------------------------- CompactionPort


def _seed_events(store: FileSessionStore, session_id: str, n: int) -> list:
    for i in range(n):
        store.append(session_id, "user_message", {"content": f"turn number {i} " * 20})
    return list(store.read_from(session_id))


async def test_no_summarizer_falls_back_to_deterministic_digest(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    events = _seed_events(store, "s1", 20)
    port = MechanicalCompactionPort()
    result = await port.compact(events, pins=[], keep_recent_tokens=20, session_store=store)
    assert result is not None
    assert "folded" in result.summary
    assert result.tokens_after < result.tokens_before
    assert result.from_seq == events[0].seq
    assert result.to_seq < events[-1].seq


async def test_too_little_history_returns_none_not_error(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    events = _seed_events(store, "s1", 1)
    port = MechanicalCompactionPort()
    result = await port.compact(events, pins=[], keep_recent_tokens=10_000, session_store=store)
    assert result is None


async def test_non_shrinking_summary_is_rejected(tmp_path: Path):
    class _UselessSummarizer:
        async def summarize(self, text: str, *, budget_tokens: int) -> str:
            return text + " " + text  # deliberately larger than the source

    store = FileSessionStore(tmp_path)
    events = _seed_events(store, "s1", 20)
    port = MechanicalCompactionPort(summarizer=_UselessSummarizer())
    with pytest.raises(CompactionRejected):
        await port.compact(events, pins=[], keep_recent_tokens=20, session_store=store)


async def test_retry_wrapper_gives_up_after_bounded_attempts(tmp_path: Path):
    class _AlwaysUselessSummarizer:
        async def summarize(self, text: str, *, budget_tokens: int) -> str:
            return text * 2

    store = FileSessionStore(tmp_path)
    events = _seed_events(store, "s1", 20)
    port = MechanicalCompactionPort(summarizer=_AlwaysUselessSummarizer())
    result = await compact_with_retry_budget(
        port, events, pins=[], keep_recent_tokens=20, session_store=store, max_retries=2
    )
    assert result is None  # never loops forever; gives up cleanly


async def test_pins_never_enter_the_summarized_text(tmp_path: Path):
    """The port has no parameter through which pins could leak into the
    folded text or the summarizer call — this asserts that by construction:
    a pin string planted nowhere in the events never appears in the summary
    even when it is passed to `compact`."""
    store = FileSessionStore(tmp_path)
    events = _seed_events(store, "s1", 20)
    pins = ["CONSTITUTION-SECRET-MARKER-XYZ"]
    port = MechanicalCompactionPort()
    result = await port.compact(events, pins=pins, keep_recent_tokens=20, session_store=store)
    assert result is not None
    assert "CONSTITUTION-SECRET-MARKER-XYZ" not in result.summary


async def test_compaction_result_summary_recoverable_from_archive(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    events = _seed_events(store, "s1", 20)
    port = MechanicalCompactionPort()
    result = await port.compact(events, pins=[], keep_recent_tokens=20, session_store=store)
    assert result is not None
    archived = store.read_archived(result.summary_ref)
    assert "turn number 0" in archived


async def test_should_compact_threshold():
    port = MechanicalCompactionPort(pressure_threshold=0.5)
    from sciteam.token_meter import SimpleTokenMeter as STM

    below = STM.from_provider_usage(prompt_tokens=100, completion_tokens=0)
    above = STM.from_provider_usage(prompt_tokens=600, completion_tokens=0)
    assert not port.should_compact(below, window=1000)
    assert port.should_compact(above, window=1000)


# -------------------------------------------------------------- overflow


def test_classify_context_overflow():
    assert classify_error(Exception("Error code: 400 - context_length_exceeded")) == (
        "context_overflow"
    )
    assert classify_error(Exception("maximum context length is 128000 tokens")) == (
        "context_overflow"
    )


def test_classify_rate_limit():
    class _RateLimited(Exception):
        status_code = 429

    assert classify_error(_RateLimited("slow down")) == "rate_limited"


def test_classify_timeout():
    assert classify_error(TimeoutError("request timed out")) == "timeout"


def test_classify_server_error():
    class _ServerErr(Exception):
        status_code = 503

    assert classify_error(_ServerErr("service unavailable")) == "server_error"


def test_classify_unknown_falls_closed_not_guessed():
    assert classify_error(Exception("something bizarre happened")) == "unknown"


def test_overflow_policy_compacts_once_then_gives_up():
    policy = OverflowRecoveryPolicy(max_attempts=1)
    exc = Exception("context_length_exceeded")
    first = policy.on_error(exc)
    assert first.action == "compact_retry"
    assert first.attempt == 1
    second = policy.on_error(exc)
    assert second.action == "give_up"
    assert second.attempt == 2


def test_overflow_policy_rejects_non_overflow_errors():
    policy = OverflowRecoveryPolicy()
    with pytest.raises(ValueError):
        policy.on_error(Exception("totally unrelated"))


def test_overflow_policy_reset_allows_fresh_budget():
    policy = OverflowRecoveryPolicy(max_attempts=1)
    exc = Exception("context_length_exceeded")
    policy.on_error(exc)
    policy.on_error(exc)
    policy.reset()
    fresh = policy.on_error(exc)
    assert fresh.action == "compact_retry"
    assert fresh.attempt == 1
