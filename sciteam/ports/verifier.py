"""Verifier port: attests closure gates for a claim.

A verdict is produced only by a trusted verifier (a frozen script / an
independent audit routine), never by a worker that could have an incentive to
pass its own claim. Gate names are open vocabulary (e.g. artifact_closed,
semantic_closed, statistically_closed) supplied by policy, not the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class VerdictRecord:
    claim_id: str
    gate: str
    passed: bool
    produced_by: str
    produced_at: str = ""
    evidence_refs: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "gate": self.gate,
            "passed": self.passed,
            "produced_by": self.produced_by,
            "produced_at": self.produced_at,
            "evidence_refs": list(self.evidence_refs),
            "detail": self.detail,
        }


@runtime_checkable
class VerifierPort(Protocol):
    def verify(
        self,
        *,
        claim_id: str,
        gate: str,
        evidence: dict[str, Any],
    ) -> VerdictRecord: ...
