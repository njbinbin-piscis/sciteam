"""Science-level plan revision: new hyp → build when frozen eval is negative.

This is a *PlanReviser* policy: templates and id stems come from the ResearchPlan
(or campaign wiring that copies plan nodes). The engine never branches on
science vocabulary; this module only emits opaque DAG nodes under max_revisions.

Mission completion stays verifier-owned artifact presence; scientific failure
triggers *new* DAG nodes — not heartbeat thrash on ``/success == true``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _stem(template: dict[str, Any], fallback: str) -> str:
    raw = str(template.get("id") or fallback).strip() or fallback
    # Strip revision suffixes if a prior revision id was used as template.
    base = raw.rsplit("_r", 1)[0]
    return base or fallback


def _read_build_success(
    completed: list[dict[str, Any]],
    *,
    build_stem: str,
) -> tuple[str | None, bool | None]:
    """Return (build_mission_id, success) for the latest build-like outcome."""
    latest_id: str | None = None
    success: bool | None = None
    for rec in completed:
        mid = str(rec.get("mission_id") or "")
        if not (mid == build_stem or mid.startswith(f"{build_stem}_") or mid.startswith(build_stem)):
            # Also honor explicit plan tag on completed records if present.
            if str(rec.get("science_loop_role") or "") != "build":
                continue
        latest_id = mid
        if "scientific_success" in rec:
            success = bool(rec.get("scientific_success"))
            continue
        art = rec.get("artifact_path") or ""
        if art and Path(art).is_file():
            try:
                data = json.loads(Path(art).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if isinstance(data, dict) and "success" in data:
                success = bool(data.get("success"))
    return latest_id, success


class ScienceLoopReviser:
    """Inject another hyp+build cycle after a negative frozen eval.

    ``hyp_template`` / ``build_template`` must be mission dicts taken from the
    ResearchPlan (institution), not invented by the engine.
    """

    def __init__(
        self,
        *,
        problem_id: str,
        hyp_template: dict[str, Any],
        build_template: dict[str, Any],
        package_id: str = "m_package",
    ) -> None:
        self._problem_id = problem_id
        self._hyp_template = hyp_template
        self._build_template = build_template
        self._package_id = package_id
        self._hyp_stem = _stem(hyp_template, "m_hyp")
        self._build_stem = _stem(build_template, "m_build")
        self.calls = 0

    def revise(
        self,
        *,
        kb: Any = None,
        completed: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        del kb
        self.calls += 1
        build_id, success = _read_build_success(
            completed, build_stem=self._build_stem
        )
        if not build_id or success is not False:
            return []
        unresolved_builds = [
            n
            for n in nodes
            if str(n.get("id") or "").startswith(self._build_stem) and not n.get("resolved")
        ]
        if unresolved_builds:
            return []

        n = sum(1 for n in nodes if str(n.get("id") or "").startswith(self._hyp_stem)) + 1
        hyp_id = f"{self._hyp_stem}_r{n}"
        build_new = f"{self._build_stem}_r{n}"
        existing = {str(n.get("id") or "") for n in nodes}
        if hyp_id in existing or build_new in existing:
            hyp_id = f"{self._hyp_stem}_r{n}_{self.calls}"
            build_new = f"{self._build_stem}_r{n}_{self.calls}"

        hyp_mission = dict(self._hyp_template)
        hyp_mission["id"] = hyp_id
        hyp_mission["goal"] = (
            f"{hyp_mission.get('goal') or 'Propose falsifiable hypotheses.'} "
            f"(science loop after {build_id} success=false; prefer a different mechanism.)"
        )

        build_mission = dict(self._build_template)
        build_mission["id"] = build_new
        meta = dict(build_mission.get("metadata") or {})
        meta["problem_id"] = self._problem_id
        meta["science_loop_role"] = "build"
        gate = dict(meta.get("artifact_gate") or {})
        if gate.get("pointer") == "/success":
            gate = {"pointer": "/problem_id", "equals": self._problem_id}
        if gate:
            meta["artifact_gate"] = gate
        build_mission["metadata"] = meta

        return [
            {
                "deps": [build_id],
                "on_fail": {"action": "continue"},
                "mission": hyp_mission,
            },
            {
                "deps": [hyp_id],
                "on_fail": {"action": "continue"},
                "mission": build_mission,
                "rewire_package_to": build_new,
                "package_id": self._package_id,
            },
        ]
