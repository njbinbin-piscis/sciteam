"""Typed evidence graph over the append-only campaign KB.

Node kinds and edge relations are OPEN VOCABULARY (research_question,
hypothesis, citation, dataset, method, code, run, result, claim; supports,
refutes, cites, uses, produces, conflicts_with, retracts, ...). The engine
stores and traverses them but never branches on the vocabulary — that lives in
assets and policy, keeping this module research-agnostic.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sciteam.kb import CampaignKB
from sciteam.ports.verifier import VerdictRecord

_NODE = "evidence_node"
_EDGE = "evidence_edge"
_VERDICT = "verdict"


class EvidenceGraph:
    def __init__(self, kb: CampaignKB) -> None:
        self._kb = kb

    # --- writes -------------------------------------------------------------
    def add_node(
        self,
        node_id: str,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        mission_id: str = "",
    ) -> None:
        self._kb.append(
            _NODE,
            {"id": node_id, "node_kind": kind, "data": data or {}},
            mission_id=mission_id,
        )

    def add_edge(
        self,
        src: str,
        dst: str,
        relation: str,
        data: dict[str, Any] | None = None,
        *,
        mission_id: str = "",
    ) -> None:
        self._kb.append(
            _EDGE,
            {"src": src, "dst": dst, "relation": relation, "data": data or {}},
            mission_id=mission_id,
        )

    def add_verdict(self, verdict: VerdictRecord, *, mission_id: str = "") -> None:
        self._kb.append(_VERDICT, verdict.to_dict(), mission_id=mission_id)

    # --- reads --------------------------------------------------------------
    def nodes(self, kind: str | None = None) -> list[dict[str, Any]]:
        out = [dict(e.data) for e in self._kb.entries(_NODE)]
        if kind is not None:
            out = [n for n in out if n.get("node_kind") == kind]
        return out

    def edges(self, relation: str | None = None) -> list[dict[str, Any]]:
        out = [dict(e.data) for e in self._kb.entries(_EDGE)]
        if relation is not None:
            out = [e for e in out if e.get("relation") == relation]
        return out

    def edges_with_relations(self, relations: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(relations)
        return [e for e in self.edges() if e.get("relation") in wanted]

    def verdicts(self, claim_id: str | None = None) -> list[dict[str, Any]]:
        out = [dict(e.data) for e in self._kb.entries(_VERDICT)]
        if claim_id is not None:
            out = [v for v in out if v.get("claim_id") == claim_id]
        return out

    def closure_status(self, claim_id: str, required_gates: Iterable[str]) -> dict[str, Any]:
        """Which required gates have a passing verdict for this claim.

        Gate names are supplied by policy (e.g. from science_thresholds.yaml);
        the engine simply checks presence of a passing verdict per gate.
        """
        required = list(required_gates)
        passed: dict[str, bool] = {gate: False for gate in required}
        for verdict in self.verdicts(claim_id):
            gate = str(verdict.get("gate") or "")
            if gate in passed and bool(verdict.get("passed")):
                passed[gate] = True
        return {
            "claim_id": claim_id,
            "gates": passed,
            "closed": all(passed.values()) if required else False,
        }
