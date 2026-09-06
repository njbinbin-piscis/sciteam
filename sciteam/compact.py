"""Deterministic context compression for worker prompts.

Builds a bounded "working pack" from prior task results instead of stuffing
full history into prompts. Role profiles tune what survives compaction, but
pinned items (protocol pointers, official result references) are never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompactionProfile:
    """What a role keeps when context is squeezed."""

    keep_last_results: int = 12
    max_chars_per_item: int = 8000
    max_total_chars: int = 80000
    keep_failures: bool = False  # implementer-style roles keep error history


DEFAULT_PROFILE = CompactionProfile()

# Role-family profiles (matched by substring of the agent key, replicas share).
PROFILES: dict[str, CompactionProfile] = {
    "implementer": CompactionProfile(
        keep_last_results=20, max_chars_per_item=10000, max_total_chars=120000, keep_failures=True
    ),
    "evaluator": CompactionProfile(
        keep_last_results=20, max_chars_per_item=10000, max_total_chars=120000, keep_failures=True
    ),
    "muse": CompactionProfile(keep_last_results=16, max_chars_per_item=4000, max_total_chars=80000),
    "debater": CompactionProfile(
        keep_last_results=16, max_chars_per_item=6000, max_total_chars=100000
    ),
    "writer": CompactionProfile(
        keep_last_results=20, max_chars_per_item=16000, max_total_chars=160000
    ),
    "librarian": CompactionProfile(
        keep_last_results=20, max_chars_per_item=8000, max_total_chars=120000
    ),
    "director": CompactionProfile(
        keep_last_results=24, max_chars_per_item=10000, max_total_chars=160000
    ),
    "research_director": CompactionProfile(
        keep_last_results=24, max_chars_per_item=10000, max_total_chars=160000
    ),
    "assembler": CompactionProfile(
        keep_last_results=20, max_chars_per_item=10000, max_total_chars=120000
    ),
    "analyst": CompactionProfile(
        keep_last_results=16, max_chars_per_item=8000, max_total_chars=100000
    ),
}


def profile_for(agent_key: str, *, scale: float = 1.0) -> CompactionProfile:
    """`scale` < 1.0 tightens the profile for context-budget degradation
    (P0-4): fewer/shorter kept items, never below a usable floor."""
    lowered = agent_key.lower()
    profile = DEFAULT_PROFILE
    for fragment, candidate in PROFILES.items():
        if fragment in lowered:
            profile = candidate
            break
    if scale >= 1.0:
        return profile
    scale = max(0.2, scale)
    return CompactionProfile(
        keep_last_results=max(2, int(profile.keep_last_results * scale)),
        max_chars_per_item=max(1000, int(profile.max_chars_per_item * scale)),
        max_total_chars=max(8000, int(profile.max_total_chars * scale)),
        keep_failures=profile.keep_failures,
    )


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...[truncated]"


@dataclass
class WorkingPack:
    pins: list[str] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    seat_memory: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        sections: list[str] = []
        if self.pins:
            sections.append("## Pinned (never dropped)\n" + "\n".join(f"- {p}" for p in self.pins))
        if self.recent:
            lines = []
            for item in self.recent:
                who = item.get("agent_key", "?")
                rnd = item.get("round_index", "?")
                lines.append(f"### round {rnd} · {who}\n{item.get('result', '')}")
            sections.append("## Recent team results\n" + "\n\n".join(lines))
        if self.failures:
            lines = [
                f"- round {item.get('round_index', '?')} · {item.get('agent_key', '?')}: {item.get('error', '')}"
                for item in self.failures
            ]
            sections.append("## Failure history (kept for debugging)\n" + "\n".join(lines))
        if self.seat_memory:
            lines = []
            for item in self.seat_memory:
                outcome = item.get("outcome", "?")
                reason = item.get("reason_code", "")
                scope = item.get("scope", "seat")
                count = int(item.get("count") or 1)
                body = (
                    item.get("counterexample")
                    or item.get("effective_fix")
                    or item.get("attempted_method")
                    or ""
                )
                refs = ", ".join(str(x) for x in (item.get("evidence_refs") or []))
                tag = f"({scope}" + (f" ×{count}" if count > 1 else "") + ")"
                lines.append(
                    f"- {outcome} [{reason}] {tag}: {body}"
                    + (f" (evidence: {refs})" if refs else "")
                )
            sections.append("## Same-seat memory (evidence-linked)\n" + "\n".join(lines))
        return "\n\n".join(sections) if sections else "(no prior context)"


def build_working_pack(
    *,
    agent_key: str,
    pins: list[str] | None = None,
    task_history: list[dict[str, Any]] | None = None,
    seat_memory: list[dict[str, Any]] | None = None,
    scale: float = 1.0,
) -> WorkingPack:
    """task_history items: {agent_key, round_index, state, result, error}.

    `scale` < 1.0 tightens the role's `CompactionProfile` (P0-4 context-budget
    degradation); pins are never affected — they are copied verbatim above.
    """
    profile = profile_for(agent_key, scale=scale)
    pack = WorkingPack(pins=list(pins or []))
    pack.seat_memory = list(seat_memory or [])

    history = list(task_history or [])
    done = [t for t in history if t.get("state") == "done" and (t.get("result") or "").strip()]
    failed = [t for t in history if t.get("state") == "failed"]

    total = 0
    for item in reversed(done):  # newest first
        if len(pack.recent) >= profile.keep_last_results:
            break
        clipped = _clip(str(item.get("result") or ""), profile.max_chars_per_item)
        if total + len(clipped) > profile.max_total_chars:
            break
        total += len(clipped)
        pack.recent.append({**item, "result": clipped})
    pack.recent.reverse()

    kept_failures = (
        failed
        if profile.keep_failures
        else [item for item in failed if item.get("agent_key") == agent_key]
    )
    if kept_failures:
        for item in kept_failures[-3:]:
            pack.failures.append({**item, "error": _clip(str(item.get("error") or ""), 400)})
    return pack
