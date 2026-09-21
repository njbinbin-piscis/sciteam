"""Container guard specifications derived from institutional manifests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_CONTAINER_IMAGE = "python:3.11-slim"
DEFAULT_MEMORY_MB = 512
DEFAULT_CPUS = 1.0
DEFAULT_PIDS_LIMIT = 64
DEFAULT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ContainerGuardSpec:
    """Security and resource limits for containerized execution."""

    image: str = DEFAULT_CONTAINER_IMAGE
    network: str = "none"
    memory_mb: int = DEFAULT_MEMORY_MB
    cpus: float = DEFAULT_CPUS
    pids_limit: int = DEFAULT_PIDS_LIMIT
    read_only_root: bool = True
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    cap_drop: tuple[str, ...] = ("ALL",)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContainerGuardSpec:
        """Derive guard specifications from a manifest dictionary or config block."""
        return cls(
            image=str(data.get("image", DEFAULT_CONTAINER_IMAGE)),
            network=str(data.get("network", "none")),
            memory_mb=int(data.get("memory_mb", DEFAULT_MEMORY_MB)),
            cpus=float(data.get("cpus", DEFAULT_CPUS)),
            pids_limit=int(data.get("pids_limit", DEFAULT_PIDS_LIMIT)),
            read_only_root=bool(data.get("read_only_root", True)),
            timeout_seconds=int(data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            cap_drop=tuple(str(x) for x in data.get("cap_drop", ("ALL",))),
        )
