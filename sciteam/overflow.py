"""Provider error classification + overflow recovery — docs/SESSION-KERNEL.md §6.

Deliberately provider-agnostic: real provider SDKs raise their own exception
types and error strings, so this module classifies by duck-typed attributes
and message heuristics rather than importing any specific SDK — keeping the
Provider-injection boundary intact (this module has no `httpx`, no vendor
SDK import).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

ProviderErrorCode = Literal[
    "context_overflow",
    "rate_limited",
    "timeout",
    "invalid_request",
    "server_error",
    "unknown",
]

_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context length",
    "too many tokens",
    "context_overflow",
    "prompt is too long",
)
_RATE_LIMIT_MARKERS = ("rate_limit", "429", "too many requests")
_TIMEOUT_MARKERS = ("timeout", "timed out")
_SERVER_ERROR_MARKERS = ("500", "502", "503", "529", "server_error", "overloaded")


def classify_error(exc: Exception) -> ProviderErrorCode:
    """Best-effort classification. Unknown/ambiguous errors fall to
    ``"unknown"`` rather than being guessed into a more specific bucket —
    guessing wrong here would either mask a real overflow (never compact)
    or trigger a pointless compaction on an unrelated failure."""
    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(exc).lower()
    haystack = f"{code} {message}"

    if any(marker in haystack for marker in _OVERFLOW_MARKERS):
        return "context_overflow"
    if status_code == 429 or any(marker in haystack for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(marker in haystack for marker in _TIMEOUT_MARKERS):
        return "timeout"
    if status_code == 400 and "context_overflow" not in haystack:
        return "invalid_request"
    if (isinstance(status_code, int) and status_code >= 500) or any(
        marker in haystack for marker in _SERVER_ERROR_MARKERS
    ):
        return "server_error"
    return "unknown"


@dataclass(frozen=True, slots=True)
class OverflowOutcome:
    action: Literal["compact_retry", "give_up"]
    attempt: int
    error_code: ProviderErrorCode


class OverflowRecoveryPolicy:
    """State machine for docs/SESSION-KERNEL.md §6: one compaction pass and
    one retry on `context_overflow`; a second overflow after a compaction
    that already shrank the context is `give_up`, surfaced as a first-class
    failure — never a silently empty response (same "no fail-open"
    discipline `mission.py`'s `ContractValidator` already applies)."""

    def __init__(self, *, max_attempts: int = 1) -> None:
        self._max_attempts = max(1, max_attempts)
        self._attempts = 0

    def reset(self) -> None:
        self._attempts = 0

    def on_error(self, exc: Exception) -> OverflowOutcome:
        code = classify_error(exc)
        if code != "context_overflow":
            # Not this policy's concern — the resilience layer's normal
            # timeout/retry/circuit-breaker handling applies instead.
            raise ValueError(f"OverflowRecoveryPolicy only handles context_overflow, got {code!r}")
        self._attempts += 1
        if self._attempts > self._max_attempts:
            return OverflowOutcome(action="give_up", attempt=self._attempts, error_code=code)
        return OverflowOutcome(action="compact_retry", attempt=self._attempts, error_code=code)
