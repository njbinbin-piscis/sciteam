"""Budgeted, evidence-linked seat memory.

P1-5: two stores share the same on-disk row shape and ranking/bounding logic.

- `SeatMemoryStore`: one agent instance's own history, scoped to its
  mission work_dir (unchanged location/shape from the original scaffold).
- `CampaignMemoryStore`: per-*role* history that survives mission
  boundaries within one campaign (a fresh mission's seat is not a blank
  slate about repeated failure modes for its role).

Recall is deterministic lexical (word-overlap) top-k against the current
task text when a query is given — not embeddings, so results are
reproducible byte-for-byte given the same memory file and query — falling
back to plain recency when no query is supplied (original behavior).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Compaction: once a memory file grows past this many rows, collapse older
# same-reason_code rows into one counted record (P1-5 bounded merge) so the
# file never grows unbounded even under "commit every success" (below).
_COMPACT_ABOVE_ROWS = 60
_KEEP_RECENT_PER_CODE = 3

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


@dataclass(frozen=True)
class MemoryRecord:
    outcome: str
    work_item_id: str
    round_index: int
    counterexample: str = ""
    attempted_method: str = ""
    effective_fix: str = ""
    evidence_refs: tuple[str, ...] = ()
    reason_code: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    count: int = 1


def tokenize(text: str) -> set[str]:
    """Deterministic word-overlap tokenizer shared across P1 lexical scorers
    (memory recall here, P1-8 skill top-k selection in `llm_worker.py`) —
    not embeddings, so results are byte-reproducible given the same inputs.
    """
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


def _overlap_score(query_tokens: set[str], row: dict[str, Any]) -> int:
    if not query_tokens:
        return 0
    body = " ".join(
        str(row.get(k) or "")
        for k in ("counterexample", "attempted_method", "effective_fix", "reason_code")
    )
    return len(query_tokens & tokenize(body))


def _rank_and_bound(
    rows: list[dict[str, Any]],
    *,
    query: str,
    limit: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Deterministic retrieval: word-overlap top-k against `query` (ties
    broken by recency = later file position), or plain recency when `query`
    is blank. Always respects `limit`/`max_chars`."""
    if not rows:
        return []
    query_tokens = tokenize(query)
    if query_tokens:
        scored = [(_overlap_score(query_tokens, row), idx) for idx, row in enumerate(rows)]
        if any(score > 0 for score, _ in scored):
            scored.sort(key=lambda t: (-t[0], -t[1]))
            ordered = [rows[idx] for score, idx in scored if score > 0]
        else:
            ordered = list(reversed(rows))
    else:
        ordered = list(reversed(rows))

    out: list[dict[str, Any]] = []
    used = 0
    for row in ordered:
        rendered = json.dumps(row, ensure_ascii=False)
        if used + len(rendered) > max_chars:
            break
        used += len(rendered)
        out.append(row)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse older same-`reason_code` rows into one counted record,
    keeping the `_KEEP_RECENT_PER_CODE` newest per code intact. `rows` is
    assumed chronological (oldest first); the result stays chronological
    (sorted by `created_at`) so recency-based recall still behaves."""
    by_code: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        code = str(row.get("reason_code") or "")
        if code not in by_code:
            by_code[code] = []
            order.append(code)
        by_code[code].append(row)

    out: list[dict[str, Any]] = []
    for code in order:
        group = by_code[code]
        if len(group) <= _KEEP_RECENT_PER_CODE:
            out.extend(group)
            continue
        older, recent = group[:-_KEEP_RECENT_PER_CODE], group[-_KEEP_RECENT_PER_CODE:]
        newest_older = older[-1]
        refs: set[str] = set()
        for o in older:
            refs.update(str(r) for r in (o.get("evidence_refs") or []))
        merged = {
            **newest_older,
            "outcome": "merged",
            "evidence_refs": sorted(refs)[:6],
            "created_at": older[0].get("created_at", ""),
            "count": sum(int(o.get("count") or 1) for o in older),
        }
        out.append(merged)
        out.extend(recent)
    out.sort(key=lambda r: str(r.get("created_at") or ""))
    return out


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _append_and_maybe_compact(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    rows = _load_rows(path)
    if len(rows) > _COMPACT_ABOVE_ROWS:
        _write_rows(path, _compact_rows(rows))


class SeatMemoryStore:
    def __init__(self, work_dir: Path | str) -> None:
        self.path = Path(work_dir) / "memory" / "seat_memory.jsonl"

    def commit(self, record: MemoryRecord) -> None:
        _append_and_maybe_compact(self.path, asdict(record))

    def recall(self, *, query: str = "", limit: int = 12, max_chars: int = 12_000) -> list[dict]:
        return _rank_and_bound(_load_rows(self.path), query=query, limit=limit, max_chars=max_chars)


class CampaignMemoryStore:
    """Per-role memory shared by every mission in one campaign. Rows carry a
    `role` field so one file can serve every seat template in the campaign.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def commit(self, role: str, record: MemoryRecord) -> None:
        role = str(role or "").strip()
        if not role:
            return
        _append_and_maybe_compact(self.path, {"role": role, **asdict(record)})

    def recall(
        self, role: str, *, query: str = "", limit: int = 6, max_chars: int = 6_000
    ) -> list[dict]:
        role = str(role or "").strip()
        if not role:
            return []
        rows = [r for r in _load_rows(self.path) if str(r.get("role") or "") == role]
        return _rank_and_bound(rows, query=query, limit=limit, max_chars=max_chars)
