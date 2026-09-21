"""Tests for the RuntimePort resilience layer: timeout, retry, circuit breaker."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from sciteam.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    ResiliencePolicy,
    ResilientRuntimePort,
    RetryConfig,
    TimeoutConfig,
)
from sciteam.runtime import (
    AgentOutcome,
    AgentRunResult,
    Run,
    RunSpec,
    RunState,
    RuntimePort,
)


class _MockRuntime:
    """Configurable test fake for RuntimePort."""

    def __init__(self, outcomes: list[Any]) -> None:
        # outcomes can be AgentRunResult, Exception, or "hang"
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def version(self) -> str:
        return "mock_runtime_v1"

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
        self.calls.append(
            {
                "agent_id": agent_id,
                "task": task,
                "work_dir": work_dir,
                "parent_run_id": parent_run_id,
                "depth": depth,
                "context": context,
            }
        )
        if not self._outcomes:
            raise RuntimeError("mock ran out of scripted outcomes")
        item = self._outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        if item == "hang":
            await asyncio.sleep(10.0)
            return _ok_result("woke_up")
        return item


def _ok_result(output: str = "done") -> AgentRunResult:
    return AgentRunResult(
        run=Run(run_id="r1", spec=RunSpec(kind="test", input="t"), state=RunState.SUCCEEDED),
        output=output,
        outcome=AgentOutcome.OK,
    )


def _recoverable_result(diag: str = "try again") -> AgentRunResult:
    return AgentRunResult(
        run=Run(run_id="r1", spec=RunSpec(kind="test", input="t"), state=RunState.FAILED),
        output=diag,
        outcome=AgentOutcome.RECOVERABLE,
        reason_code="transient_glitch",
        diagnosis=diag,
    )


def _fatal_result(diag: str = "unrecoverable") -> AgentRunResult:
    return AgentRunResult(
        run=Run(run_id="r1", spec=RunSpec(kind="test", input="t"), state=RunState.FAILED),
        output=diag,
        outcome=AgentOutcome.FATAL,
        reason_code="fatal_error",
        diagnosis=diag,
    )


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        assert cb.state == CircuitState.CLOSED
        allowed, _ = cb.can_attempt()
        assert allowed

    def test_trips_to_open_after_reaching_threshold(self) -> None:
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=60.0))
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        allowed, reason = cb.can_attempt()
        assert not allowed
        assert "circuit breaker is open" in reason
        assert cb.trip_count == 1

    def test_fatal_failure_trips_immediately(self) -> None:
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        cb.record_failure(fatal=True)
        assert cb.state == CircuitState.OPEN
        assert cb.trip_count == 1

    def test_transitions_to_half_open_after_cooldown(self) -> None:
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_probe_success_closes_circuit(self) -> None:
        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05, success_threshold=1)
        )
        cb.record_failure()
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.consecutive_failures == 0

    def test_half_open_probe_failure_reopens_circuit(self) -> None:
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.trip_count == 2


class TestResilientRuntimePort:
    @pytest.mark.asyncio
    async def test_successful_call_passes_through(self) -> None:
        mock = _MockRuntime([_ok_result("all good")])
        port = ResilientRuntimePort(mock)
        assert isinstance(port, RuntimePort)
        assert port.version() == "mock_runtime_v1"

        res = await port.run_subagent(agent_id="a1", task="do stuff", work_dir="/tmp")
        assert res.outcome == AgentOutcome.OK
        assert res.output == "all good"
        assert len(mock.calls) == 1
        assert port.metrics()["successful_calls"] == 1

    @pytest.mark.asyncio
    async def test_retries_transient_recoverable_outcome_and_succeeds(self) -> None:
        mock = _MockRuntime([_recoverable_result("flaky 1"), _ok_result("healed")])
        policy = ResiliencePolicy(
            retry=RetryConfig(max_retries=2, base_delay=0.01, jitter=False),
        )
        port = ResilientRuntimePort(mock, policy=policy)

        res = await port.run_subagent(agent_id="a1", task="work", work_dir="/tmp")
        assert res.outcome == AgentOutcome.OK
        assert res.output == "healed"
        assert len(mock.calls) == 2
        assert port.metrics()["retries_performed"] == 1

    @pytest.mark.asyncio
    async def test_retries_retryable_exception_and_succeeds(self) -> None:
        mock = _MockRuntime([ConnectionError("connection reset"), _ok_result("reconnected")])
        policy = ResiliencePolicy(
            retry=RetryConfig(max_retries=2, base_delay=0.01, jitter=False),
        )
        port = ResilientRuntimePort(mock, policy=policy)

        res = await port.run_subagent(agent_id="a1", task="work", work_dir="/tmp")
        assert res.outcome == AgentOutcome.OK
        assert res.output == "reconnected"
        assert len(mock.calls) == 2

    @pytest.mark.asyncio
    async def test_non_retryable_exception_fails_fast_with_fatal(self) -> None:
        mock = _MockRuntime([ValueError("bad payload")])
        policy = ResiliencePolicy(
            retry=RetryConfig(max_retries=3, base_delay=0.01),
        )
        port = ResilientRuntimePort(mock, policy=policy)

        res = await port.run_subagent(agent_id="a1", task="work", work_dir="/tmp")
        assert res.outcome == AgentOutcome.FATAL
        assert res.reason_code == "runtime_valueerror"
        assert len(mock.calls) == 1  # Not retried!

    @pytest.mark.asyncio
    async def test_per_attempt_timeout_retries_and_returns_recoverable(self) -> None:
        mock = _MockRuntime(["hang", "hang"])
        policy = ResiliencePolicy(
            timeout=TimeoutConfig(per_attempt_timeout_s=0.05),
            retry=RetryConfig(max_retries=1, base_delay=0.01, jitter=False),
        )
        port = ResilientRuntimePort(mock, policy=policy)

        res = await port.run_subagent(agent_id="a1", task="slow query", work_dir="/tmp")
        assert res.outcome == AgentOutcome.RECOVERABLE
        assert res.reason_code == "runtime_timeout"
        assert len(mock.calls) == 2
        assert port.metrics()["timeouts_encountered"] == 2

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_calls_when_open(self) -> None:
        mock = _MockRuntime(
            [
                ConnectionError("down 1"),
                ConnectionError("down 2"),
                _ok_result("never reached"),
            ]
        )
        policy = ResiliencePolicy(
            retry=RetryConfig(max_retries=0),
            circuit_breaker=CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=30.0),
        )
        port = ResilientRuntimePort(mock, policy=policy)

        # Call 1 fails
        r1 = await port.run_subagent(agent_id="a1", task="t1", work_dir="/tmp")
        assert r1.outcome == AgentOutcome.RECOVERABLE

        # Call 2 fails -> trips circuit
        r2 = await port.run_subagent(agent_id="a1", task="t2", work_dir="/tmp")
        assert r2.outcome == AgentOutcome.RECOVERABLE
        assert port.circuit_breaker.state == CircuitState.OPEN

        # Call 3 is rejected immediately without touching mock
        r3 = await port.run_subagent(agent_id="a1", task="t3", work_dir="/tmp")
        assert r3.outcome == AgentOutcome.FATAL
        assert r3.reason_code == "circuit_breaker_open"
        assert len(mock.calls) == 2  # mock was not called a 3rd time!

    def test_resilience_policy_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCITEAM_RUNTIME_TIMEOUT", "45.5")
        monkeypatch.setenv("SCITEAM_RUNTIME_MAX_RETRIES", "4")
        monkeypatch.setenv("SCITEAM_CIRCUIT_BREAKER_FAILURES", "5")
        monkeypatch.setenv("SCITEAM_CIRCUIT_BREAKER_COOLDOWN", "15.0")

        policy = ResiliencePolicy.from_env()
        assert policy.timeout.per_attempt_timeout_s == 45.5
        assert policy.retry.max_retries == 4
        assert policy.circuit_breaker.failure_threshold == 5
        assert policy.circuit_breaker.cooldown_seconds == 15.0
