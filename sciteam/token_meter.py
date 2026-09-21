"""``TokenMeter`` — docs/SESSION-KERNEL.md §4.

Distinguishes provider-reported truth from a character-count heuristic and
never lets the second masquerade as the first. This is the session-layer
expression of the same honesty discipline `provenance.py` already applies to
environment fingerprints (`unpinnable` is a first-class field there; here
`estimated` plays the same role).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sciteam.session import ProjectedContext

#: crude but conservative fallback used only when a provider does not (yet)
#: report real usage for a given call; deliberately documented as a rough
#: heuristic, never presented as ground truth (see `TokenReading.estimated`).
_CHARS_PER_TOKEN_HEURISTIC = 4

TokenSource = Literal["provider", "heuristic_chars_per_token"]


@dataclass(frozen=True, slots=True)
class TokenReading:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    estimated: bool
    source: TokenSource

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def estimate_tokens_from_chars(text: str) -> int:
    return estimate_tokens_from_char_count(len(text))


def estimate_tokens_from_char_count(n_chars: int) -> int:
    return max(0, n_chars // _CHARS_PER_TOKEN_HEURISTIC)


def _context_chars(projected: ProjectedContext) -> int:
    total = 0
    for msg in projected.messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


class SimpleTokenMeter:
    """Default `TokenMeter`: heuristic unless the caller feeds real usage
    back in via `record_call`. Keeps a short rolling history so `pressure`
    can be asked either about a fresh projection or about the last call this
    meter actually observed."""

    def __init__(self) -> None:
        self._history: list[TokenReading] = []

    def measure(self, projected: ProjectedContext) -> TokenReading:
        chars = _context_chars(projected)
        return TokenReading(
            prompt_tokens=estimate_tokens_from_char_count(chars),
            completion_tokens=0,
            cached_tokens=0,
            estimated=True,
            source="heuristic_chars_per_token",
        )

    def record_call(self, reading: TokenReading) -> None:
        self._history.append(reading)

    def pressure(self, reading: TokenReading, window: int) -> float:
        if window <= 0:
            raise ValueError("window must be positive")
        return reading.total_tokens / window

    @property
    def history(self) -> tuple[TokenReading, ...]:
        return tuple(self._history)

    @classmethod
    def from_provider_usage(
        cls,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
    ) -> TokenReading:
        """Build a real (non-estimated) reading from a provider's own usage
        payload — the only path by which `estimated=False` may occur."""
        return TokenReading(
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            cached_tokens=int(cached_tokens),
            estimated=False,
            source="provider",
        )
