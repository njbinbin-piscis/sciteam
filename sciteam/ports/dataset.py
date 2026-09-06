"""Dataset port: license-clear public datasets materialised deterministically."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class DatasetManifest:
    """Provenance for a materialised dataset (or split)."""

    dataset_id: str
    source: str
    version: str
    content_hash: str
    license: str
    url: str | None = None
    local_path: str | None = None
    splits: dict[str, str] = field(default_factory=dict)  # split name -> hash
    retrieved_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "version": self.version,
            "content_hash": self.content_hash,
            "license": self.license,
            "url": self.url,
            "local_path": self.local_path,
            "splits": dict(self.splits),
            "retrieved_at": self.retrieved_at,
            "notes": self.notes,
        }


@runtime_checkable
class DatasetPort(Protocol):
    def manifest(self, dataset_id: str) -> DatasetManifest: ...

    def materialize(self, dataset_id: str, dest: str) -> DatasetManifest: ...
