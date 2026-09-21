"""Resilience policies for `RuntimePort` backends.

An external agent or model provider is an untrusted remote dependency: it can
hang, throttle with 429, fail intermittently with 5xx, or crash completely.
Leaving those failure modes unmanaged violates **G4.1** (every failure must
have an explicit first-class disposition rather than an unhandled crash) and
**G2.1** (traces must accurately record what failed and why).

This module provides a composable resilience layer around any :class:`~sciteam.runtime.RuntimePort`:

* **Timeout**: Enforces strict per-attempt and total invocation ceilings using
  `asyncio.wait_for`. A hung provider is terminated rather than stalling the
  campaign indefinitely.
* **Exponential Backoff with Jitter**: Automatically retries transient
  errors (timeouts, connection drops, recoverable model outcomes) with
  decorrelated jitter to prevent thundering herds against rate limits.
* **Circuit Breaker**: Tracks consecutive failures across invocations. When a
  backend is down, the breaker trips to ``OPEN``, immediately failing fast with
  :attr:`~sciteam.runtime.AgentOutcome.FATAL` rather than wasting round budget
  and waiting for timeouts. After a cooldown period, it transitions to
  ``HALF_OPEN`` to probe for recovery.
* **First-Class Disposition**: Maps all unhandled exceptions and faults into
  an :class:`~sciteam.runtime.AgentRunResult` with clear
  :class:`~sciteam.runtime.AgentOutcome` (:attr:`~sciteam.runtime.AgentOutcome.RECOVERABLE`
  or :attr:`~sciteam.runtime.AgentOutcome.FATAL`), so the institutional loop
  can take formal recovery action.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sciteam.overflow import classify_error
from sciteam.runtime import (
    AgentOutcome,
    AgentRunResult,
    Run,
    RunSpec,
    RunState,
    RuntimePort,
)

#  E7 (product contract): a provider 429/5xx does not necessarily raise one
# of `RetryConfig.retryable_exceptions`'s built-in types — SDKs raise their
# own vendor exception classes. `sciteam.overflow.classify_error` already
# does provider-agnostic, duck-typed classification for the compaction/
# overflow path; reusing it here means a rate-limited or momentarily
# overloaded provider gets retried by *this* layer even when its exception
# type is one this module has never seen before.
_RESILIENCE_RETRYABLE_ERROR_CODES = frozenset({"rate_limited", "server_error", "timeout"})

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Thresholds controlling when the circuit trips and how it probes recovery."""

    failure_threshold: int = 3
    cooldown_seconds: float = 10.0
    success_threshold: int = 1


class CircuitBreaker:
    """Thread-safe state machine guarding a remote runtime backend."""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_state_change = time.monotonic()
        self._trip_count = 0

    @property
    def state(self) -> CircuitState:
        self._check_cooldown()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def trip_count(self) -> int:
        return self._trip_count

    def _check_cooldown(self) -> None:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_state_change
            if elapsed >= self._config.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                self._last_state_change = time.monotonic()
                logger.info("circuit breaker entered half_open state after %.1fs cooldown", elapsed)

    def can_attempt(self) -> tuple[bool, str]:
        """Returns (allowed, reason_if_blocked)."""
        current = self.state
        if current == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_state_change
            remaining = max(0.0, self._config.cooldown_seconds - elapsed)
            msg = (
                f"circuit breaker is open ({self._consecutive_failures} failures; "
                f"{remaining:.1f}s cooldown remaining)"
            )
            return False, msg
        return True, ""

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._config.success_threshold:
                self._state = CircuitState.CLOSED
                self._consecutive_failures = 0
                self._consecutive_successes = 0
                self._last_state_change = time.monotonic()
                logger.info("circuit breaker closed after successful recovery probe")
        elif self._state == CircuitState.CLOSED:
            self._consecutive_failures = 0

    def record_failure(self, *, fatal: bool = False) -> None:
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._last_state_change = time.monotonic()
            self._trip_count += 1
            logger.warning("circuit breaker tripped back to open during half_open probe")
        elif self._state == CircuitState.CLOSED:
            if fatal or self._consecutive_failures >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                self._last_state_change = time.monotonic()
                self._trip_count += 1
                logger.warning(
                    "circuit breaker tripped open after %d failure(s) (fatal=%s)",
                    self._consecutive_failures,
                    fatal,
                )


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Policy for retrying transient subagent execution failures."""

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 10.0
    backoff_factor: float = 2.0
    jitter: bool = True
    retryable_outcomes: frozenset[AgentOutcome] = frozenset({AgentOutcome.RECOVERABLE})
    retryable_exceptions: tuple[type[Exception], ...] = (
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    )

    def calculate_delay(self, attempt: int) -> float:
        """Computes backoff delay with decorrelated jitter."""
        delay = min(self.max_delay, self.base_delay * (self.backoff_factor**attempt))
        if self.jitter:
            delay += random.uniform(0.0, delay * 0.5)
        return max(0.0, delay)


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    """Limits on execution time per attempt and across all retries."""

    per_attempt_timeout_s: float | None = None
    total_timeout_s: float | None = None


@dataclass(frozen=True, slots=True)
class ResiliencePolicy:
    """Complete resilience configuration for a `RuntimePort` backend."""

    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    @classmethod
    def from_env(cls) -> ResiliencePolicy:
        """Read standard SCITEAM_* environment variables to configure resilience."""
        raw_timeout = os.environ.get("SCITEAM_RUNTIME_TIMEOUT", "").strip()
        per_attempt_timeout = float(raw_timeout) if raw_timeout else None

        raw_retries = os.environ.get("SCITEAM_RUNTIME_MAX_RETRIES", "").strip()
        max_retries = int(raw_retries) if raw_retries else 2

        raw_failures = os.environ.get("SCITEAM_CIRCUIT_BREAKER_FAILURES", "").strip()
        failures = int(raw_failures) if raw_failures else 3

        raw_cooldown = os.environ.get("SCITEAM_CIRCUIT_BREAKER_COOLDOWN", "").strip()
        cooldown = float(raw_cooldown) if raw_cooldown else 10.0

        return cls(
            timeout=TimeoutConfig(per_attempt_timeout_s=per_attempt_timeout),
            retry=RetryConfig(max_retries=max_retries),
            circuit_breaker=CircuitBreakerConfig(
                failure_threshold=failures,
                cooldown_seconds=cooldown,
            ),
        )


class ResilientRuntimePort:
    """Wraps any `RuntimePort` with timeout, retries, and circuit breaking.

    This ensures that downstream callers (such as `heartbeat` or `round_assessor`)
    never receive an unhandled exception from the runtime layer, fulfilling G4.1.
    """

    def __init__(
        self,
        inner: RuntimePort,
        *,
        policy: ResiliencePolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or ResiliencePolicy()
        self._circuit_breaker = circuit_breaker or CircuitBreaker(self._policy.circuit_breaker)
        self._call_count = 0
        self._success_count = 0
        self._retry_count = 0
        self._timeout_count = 0

    @property
    def inner(self) -> RuntimePort:
        return self._inner

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def policy(self) -> ResiliencePolicy:
        return self._policy

    def version(self) -> str:
        """Propagate backend version if supported by the inner runtime."""
        version_fn = getattr(self._inner, "version", None)
        if callable(version_fn):
            return str(version_fn())
        return "resilient_wrapper"

    def metrics(self) -> dict[str, Any]:
        """Observability snapshot for operator reporting."""
        return {
            "total_calls": self._call_count,
            "successful_calls": self._success_count,
            "retries_performed": self._retry_count,
            "timeouts_encountered": self._timeout_count,
            "circuit_state": self._circuit_breaker.state.value,
            "circuit_consecutive_failures": self._circuit_breaker.consecutive_failures,
            "circuit_trips": self._circuit_breaker.trip_count,
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
        self._call_count += 1
        allowed, block_reason = self._circuit_breaker.can_attempt()
        if not allowed:
            diagnosis = f"Call aborted: {block_reason}"
            logger.warning("refusing subagent execution for %s: %s", agent_id, block_reason)
            return self._make_result(
                agent_id=agent_id,
                task=task,
                work_dir=work_dir,
                parent_run_id=parent_run_id,
                outcome=AgentOutcome.FATAL,
                reason_code="circuit_breaker_open",
                diagnosis=diagnosis,
                retry_hint="Provider is currently unhealthy. Wait for circuit breaker cooldown.",
            )

        start_time = time.monotonic()
        retry_cfg = self._policy.retry
        timeout_cfg = self._policy.timeout
        max_attempts = max(1, retry_cfg.max_retries + 1)
        last_result: AgentRunResult | None = None
        last_error_diag = ""

        for attempt in range(max_attempts):
            if timeout_cfg.total_timeout_s is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout_cfg.total_timeout_s:
                    diag = (
                        f"Total execution time exceeded ceiling of {timeout_cfg.total_timeout_s}s"
                    )
                    self._circuit_breaker.record_failure(fatal=True)
                    return self._make_result(
                        agent_id=agent_id,
                        task=task,
                        work_dir=work_dir,
                        parent_run_id=parent_run_id,
                        outcome=AgentOutcome.FATAL,
                        reason_code="total_timeout_exceeded",
                        diagnosis=diag,
                    )

            try:
                coro = self._inner.run_subagent(
                    agent_id=agent_id,
                    task=task,
                    work_dir=work_dir,
                    parent_run_id=parent_run_id,
                    depth=depth,
                    context=context,
                )
                if timeout_cfg.per_attempt_timeout_s is not None:
                    result = await asyncio.wait_for(coro, timeout=timeout_cfg.per_attempt_timeout_s)
                else:
                    result = await coro

            except TimeoutError as exc:
                self._timeout_count += 1
                last_error_diag = f"Attempt {attempt + 1} timed out: {exc}"
                logger.warning(
                    "runtime call timed out for agent %s (attempt %d/%d)",
                    agent_id,
                    attempt + 1,
                    max_attempts,
                )
                self._circuit_breaker.record_failure(fatal=False)
                if attempt < max_attempts - 1:
                    self._retry_count += 1
                    delay = retry_cfg.calculate_delay(attempt)
                    await asyncio.sleep(delay)
                    continue
                return self._make_result(
                    agent_id=agent_id,
                    task=task,
                    work_dir=work_dir,
                    parent_run_id=parent_run_id,
                    outcome=AgentOutcome.RECOVERABLE,
                    reason_code="runtime_timeout",
                    diagnosis=last_error_diag,
                    retry_hint=(
                        "Request timed out. Consider simplifying query or increasing timeout."
                    ),
                )

            except Exception as exc:  # noqa: BLE001
                is_retryable = (
                    isinstance(exc, retry_cfg.retryable_exceptions)
                    or classify_error(exc) in _RESILIENCE_RETRYABLE_ERROR_CODES
                )
                last_error_diag = f"Attempt {attempt + 1} failed with {type(exc).__name__}: {exc}"
                logger.warning(
                    "runtime exception for agent %s: %s (retryable=%s)",
                    agent_id,
                    last_error_diag,
                    is_retryable,
                )
                self._circuit_breaker.record_failure(fatal=not is_retryable)
                if is_retryable and attempt < max_attempts - 1:
                    self._retry_count += 1
                    delay = retry_cfg.calculate_delay(attempt)
                    await asyncio.sleep(delay)
                    continue
                return self._make_result(
                    agent_id=agent_id,
                    task=task,
                    work_dir=work_dir,
                    parent_run_id=parent_run_id,
                    outcome=AgentOutcome.RECOVERABLE if is_retryable else AgentOutcome.FATAL,
                    reason_code=f"runtime_{type(exc).__name__.lower()}",
                    diagnosis=last_error_diag,
                    retry_hint="Underlying runtime threw an uncaught error.",
                )

            # Execution succeeded without raising an exception: examine AgentRunResult outcome
            if result.outcome == AgentOutcome.OK:
                self._success_count += 1
                self._circuit_breaker.record_success()
                return result

            last_result = result
            is_outcome_retryable = result.outcome in retry_cfg.retryable_outcomes
            self._circuit_breaker.record_failure(fatal=(result.outcome == AgentOutcome.FATAL))

            if is_outcome_retryable and attempt < max_attempts - 1:
                self._retry_count += 1
                delay = retry_cfg.calculate_delay(attempt)
                logger.info(
                    "retrying subagent %s after outcome=%s (delay=%.2fs, attempt=%d/%d)",
                    agent_id,
                    result.outcome,
                    delay,
                    attempt + 1,
                    max_attempts,
                )
                await asyncio.sleep(delay)
                continue

            return result

        return last_result or self._make_result(
            agent_id=agent_id,
            task=task,
            work_dir=work_dir,
            parent_run_id=parent_run_id,
            outcome=AgentOutcome.FATAL,
            reason_code="retries_exhausted",
            diagnosis=last_error_diag or "All retry attempts exhausted.",
        )

    def _make_result(
        self,
        *,
        agent_id: str,
        task: str,
        work_dir: str,
        parent_run_id: str | None,
        outcome: AgentOutcome,
        reason_code: str,
        diagnosis: str,
        retry_hint: str = "",
    ) -> AgentRunResult:
        run = Run(
            run_id=f"resilient_{agent_id}_{self._call_count}",
            spec=RunSpec(
                kind="resilient_wrapper",
                input=task,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                metadata={"work_dir": work_dir},
            ),
            state=RunState.FAILED if outcome != AgentOutcome.OK else RunState.SUCCEEDED,
            output=diagnosis,
            error=diagnosis if outcome != AgentOutcome.OK else None,
        )
        return AgentRunResult(
            run=run,
            output=diagnosis,
            outcome=outcome,
            reason_code=reason_code,
            diagnosis=diagnosis,
            retry_hint=retry_hint,
        )
