"""Campaign knowledge base: typed, append-only, survives mission boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class KBEntry:
    seq: int
    kind: str
    data: dict[str, Any]
    mission_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "mission_id": self.mission_id,
            "created_at": self.created_at,
            "data": self.data,
        }


class CampaignKB:
    """Append-only JSONL store. Entry kinds are open vocabulary (claim,
    citation, result, decision, mission_outcome, ...); the engine never
    branches on them."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[KBEntry] = []
        if self._path.is_file():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                self._entries.append(
                    KBEntry(
                        seq=int(raw["seq"]),
                        kind=str(raw["kind"]),
                        data=dict(raw.get("data") or {}),
                        mission_id=str(raw.get("mission_id") or ""),
                        created_at=str(raw.get("created_at") or ""),
                    )
                )

    @property
    def path(self) -> Path:
        return self._path

    def append(self, kind: str, data: dict[str, Any], *, mission_id: str = "") -> KBEntry:
        kind = kind.strip()
        if not kind:
            raise ValueError("kb entry kind must be non-empty")
        entry = KBEntry(
            seq=len(self._entries) + 1, kind=kind, data=dict(data), mission_id=mission_id
        )
        self._entries.append(entry)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        return entry

    def entries(self, kind: str | None = None, *, mission_id: str | None = None) -> list[KBEntry]:
        out = list(self._entries)
        if kind is not None:
            out = [e for e in out if e.kind == kind]
        if mission_id is not None:
            out = [e for e in out if e.mission_id == mission_id]
        return out

    def latest(self, kind: str) -> KBEntry | None:
        for entry in reversed(self._entries):
            if entry.kind == kind:
                return entry
        return None

    def summary(self, *, max_per_kind: int = 5) -> dict[str, Any]:
        """Compact per-kind digest for planner / worker context packs."""
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for entry in self._entries:
            bucket = by_kind.setdefault(entry.kind, [])
            bucket.append({"seq": entry.seq, "mission_id": entry.mission_id, "data": entry.data})
        return {
            "total_entries": len(self._entries),
            "kinds": {
                kind: {"count": len(items), "latest": items[-max_per_kind:]}
                for kind, items in by_kind.items()
            },
        }
