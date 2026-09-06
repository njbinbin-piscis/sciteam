"""Downstream counterexamples reopen and supersede generic DAG nodes."""

from __future__ import annotations

import json
from pathlib import Path

from sciteam.adaptive_planner import AdaptiveCampaignPlanner
from sciteam.campaign import BudgetClock, CampaignBudget
from sciteam.coordination import MANDATORY_INSTITUTIONS, CoordinationSpec
from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome


def _node(node_id: str, deps: list[str] | None = None) -> dict:
    return {
        "deps": list(deps or []),
        "mission": {
            "id": node_id,
            "goal": f"run {node_id}",
            "paradigm": "pipeline_chain",
            "exit_contract": "results",
        },
    }


def _outcome(node_id: str, artifact: Path | None = None) -> MissionOutcome:
    return MissionOutcome(
        mission_id=node_id,
        status="completed",
        team_run_id="team",
        rounds_used=1,
        wall_clock_seconds=0.1,
        contract_ok=True,
        artifact_path=str(artifact) if artifact else "",
    )


def test_reopen_and_supersede_preserve_old_artifact(tmp_path: Path) -> None:
    planner = AdaptiveCampaignPlanner(
        {"missions": [_node("design"), _node("build", ["design"]), _node("ship", ["build"])]}
    )
    old_artifact = tmp_path / "build.json"
    old_artifact.write_text(json.dumps({"version": 1}), encoding="utf-8")
    kb = CampaignKB(tmp_path / "kb.jsonl")
    clock = BudgetClock(CampaignBudget())
    first = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    planner.observe(_outcome(first.mission.id))  # type: ignore[union-attr]
    second = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    planner.observe(_outcome(second.mission.id, old_artifact))  # type: ignore[union-attr]

    planner._add_node(_node("build_v2", ["design"]), origin="revision")
    assert planner.reopen_upstream(
        target="design",
        reason_code="downstream_counterexample",
        requested_by="audit",
    )
    assert planner.supersede(
        target="build",
        replacement="build_v2",
        reason_code="invalid_assumption",
    )

    snapshot = planner.plan_snapshot()
    build = next(node for node in snapshot["nodes"] if node["id"] == "build")
    ship = next(node for node in snapshot["nodes"] if node["id"] == "ship")
    assert build["superseded"] and build["superseded_by"] == "build_v2"
    assert ship["deps"] == ["build_v2"]
    assert json.loads(old_artifact.read_text()) == {"version": 1}
    sidecar = old_artifact.with_suffix(".json.superseded.json")
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text())["replacement"] == "build_v2"

    decision = planner.next_mission(
        last_outcome=None,
        kb=kb,
        clock=clock,
    )
    assert decision.mission is not None
    assert decision.mission.id.startswith("design_a")


def test_mandatory_feedback_institutions_cannot_be_removed() -> None:
    coordination = CoordinationSpec.from_dict({"charter": {"institutions": ["CUSTOM_REVIEW"]}})
    assert set(MANDATORY_INSTITUTIONS).issubset(coordination.charter.institutions)
    assert "CUSTOM_REVIEW" in coordination.charter.institutions
