"""Cut-point selection, ported from pi-coding-agent dist/core/compaction/compaction.js
(findValidCutPoints / findTurnStartIndex / findCutPoint)."""

from pydantic import BaseModel

from sciteam.harness.state import TURN_START_KINDS, Entry


class CutPoint(BaseModel):
    first_kept: int
    turn_start: int  # -1 if not a split turn
    is_split_turn: bool


def _is_cut_point(e: Entry) -> bool:
    # Never cut at a tool result: it must follow its assistant tool call.
    return e.msg.role in ("user", "assistant")


def _is_turn_start(e: Entry) -> bool:
    return e.kind in TURN_START_KINDS


def find_turn_start(entries: list[Entry], idx: int, start: int) -> int:
    for i in range(idx, start - 1, -1):
        if _is_turn_start(entries[i]):
            return i
    return -1


def find_cut_point(entries: list[Entry], start: int, end: int, keep_tokens: int) -> CutPoint:
    cut_points = [i for i in range(start, end) if _is_cut_point(entries[i])]
    if not cut_points:
        return CutPoint(first_kept=start, turn_start=-1, is_split_turn=False)
    acc = 0
    cut = cut_points[0]
    for i in range(end - 1, start - 1, -1):
        acc += entries[i].tokens
        if acc >= keep_tokens:
            for c in cut_points:
                if c >= i:
                    cut = c
                    break
            break
    starts_turn = _is_turn_start(entries[cut])
    turn_start = -1 if starts_turn else find_turn_start(entries, cut, start)
    return CutPoint(first_kept=cut, turn_start=turn_start,
                    is_split_turn=not starts_turn and turn_start != -1)


def tail_budget(window: int, *, fraction: float, floor: int, cap: int, max_window_fraction: float
                ) -> int:
    """Hermes lean tail: clamp(fraction × window, floor, cap), then ≤ max_window_fraction × window."""
    budget = max(floor, min(cap, int(window * fraction)))
    return min(budget, int(window * max_window_fraction))
