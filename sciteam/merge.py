"""Artifact-first merge clerk for replica deltas."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MergeDelta:
    work_item_id: str
    replica_key: str
    touched_paths: list[str] = field(default_factory=list)
    kb_assertions: list[dict[str, Any]] = field(default_factory=list)
    source_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "replica_key": self.replica_key,
            "touched_paths": list(self.touched_paths),
            "kb_assertions": list(self.kb_assertions),
            "source_dir": self.source_dir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MergeDelta:
        return cls(
            work_item_id=str(data.get("work_item_id") or ""),
            replica_key=str(data.get("replica_key") or ""),
            touched_paths=[str(p) for p in (data.get("touched_paths") or [])],
            kb_assertions=list(data.get("kb_assertions") or []),
            source_dir=str(data.get("source_dir") or ""),
        )


@dataclass
class MergeResult:
    ok: bool
    merged_paths: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    message: str = ""


class MergeClerk:
    """Copy non-conflicting files from replica into target; flag path collisions."""

    def apply(
        self,
        delta: MergeDelta,
        *,
        target_root: str | Path,
        occupied_paths: set[str] | None = None,
    ) -> MergeResult:
        target = Path(target_root)
        target.mkdir(parents=True, exist_ok=True)
        occupied = set(occupied_paths or [])
        merged: list[str] = []
        conflicts: list[str] = []
        src_root = Path(delta.source_dir) if delta.source_dir else None
        for rel in delta.touched_paths:
            rel_n = rel.lstrip("/")
            dest = target / rel_n
            if rel_n in occupied or dest.exists():
                conflicts.append(rel_n)
                continue
            if src_root is not None:
                src = src_root / rel_n
                if src.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    merged.append(rel_n)
                    occupied.add(rel_n)
                    continue
            # No source file — still record as merged metadata-only if no conflict
            merged.append(rel_n)
            occupied.add(rel_n)
        ok = not conflicts
        return MergeResult(
            ok=ok,
            merged_paths=merged,
            conflicts=conflicts,
            message="ok" if ok else f"conflicts: {', '.join(conflicts)}",
        )


def write_delta(path: Path, delta: MergeDelta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(delta.to_dict(), indent=2), encoding="utf-8")


def read_delta(path: Path) -> MergeDelta:
    return MergeDelta.from_dict(json.loads(path.read_text(encoding="utf-8")))
