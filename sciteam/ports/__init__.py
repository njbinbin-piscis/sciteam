"""Domain-neutral ports for external research IO.

These Protocols are the only sanctioned boundary between the engine and the
outside world (references, datasets, compute nodes, verifiers). Concrete
adapters live under ``harness/adapters/`` and never leak into engine logic, so
the engine stays research-agnostic (E0) and open-sourceable.
"""

from __future__ import annotations

from sciteam.ports.compute import (
    ComputeJobResult,
    ComputeJobSpec,
    ComputePort,
    JobState,
)
from sciteam.ports.dataset import DatasetManifest, DatasetPort
from sciteam.ports.literature import (
    ClaimSpan,
    LiteraturePort,
    Reference,
    SearchQuery,
    SearchResult,
)
from sciteam.ports.verifier import VerdictRecord, VerifierPort

__all__ = [
    "ClaimSpan",
    "ComputeJobResult",
    "ComputeJobSpec",
    "ComputePort",
    "DatasetManifest",
    "DatasetPort",
    "JobState",
    "LiteraturePort",
    "Reference",
    "SearchQuery",
    "SearchResult",
    "VerdictRecord",
    "VerifierPort",
]
