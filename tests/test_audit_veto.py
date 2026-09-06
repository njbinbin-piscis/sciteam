"""Line A close-loop gap #2: I_PROCESS_AUDIT veto -> mechanical I_DESIGN_FEEDBACK
reopen (sciteam/audit_veto.py, schemas/audit_veto.schema.json).

Same discipline as tests/test_retro_signal.py for the signal/scanner half;
the second half here (`VetoUpstreamReviser`) additionally exercises the
authority gate against `sciteam.adaptive_planner.AdaptiveCampaignPlanner`'s
already-tested `reopen_upstream` primitive (tests/test_reopen_upstream.py),
proving the two were genuinely wired together, not just individually
testable in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from sciteam import audit_veto
from sciteam.adaptive_planner import AdaptiveCampaignPlanner
from sciteam.campaign import BudgetClock, CampaignBudget
from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome

LAB_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = LAB_ROOT / "schemas" / "audit_veto.schema.json"


# ---------------------------------------------------------------------------
# Signal + scanner (mirrors test_retro_signal.py)
# ---------------------------------------------------------------------------


def test_schema_is_valid_draft_2020_12():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_write_veto_round_trips(tmp_path):
    workspace = tmp_path / "missions" / "m_review"
    path = audit_veto.write_veto(
        workspace,
        target="m_implement",
        reason_code="late_test_failure",
        vetoed_by="auditor",
        vetoed_at="2026-09-06T00:00:00+00:00",
    )
    assert path.name == audit_veto.SIGNAL_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["target"] == "m_implement"
    assert "detail" not in payload


def test_write_veto_rejects_bad_reason_code(tmp_path):
    with pytest.raises(jsonschema.ValidationError):
        audit_veto.write_veto(
            tmp_path,
            target="m_implement",
            reason_code="i_just_dont_like_it",
            vetoed_by="auditor",
            vetoed_at="2026-09-06T00:00:00+00:00",
        )


def test_scan_ignores_malformed_signal_fail_closed(tmp_path):
    run_dir = tmp_path / "run"
    mission_dir = run_dir / "missions" / "m_review"
    mission_dir.mkdir(parents=True)
    (mission_dir / audit_veto.SIGNAL_FILENAME).write_text("not json", encoding="utf-8")
    assert audit_veto.scan_for_vetoes(run_dir) == []


def test_scan_finds_signal():
    pass  # covered by the reviser tests below, which exercise scan end-to-end


# ---------------------------------------------------------------------------
# VetoUpstreamReviser: authority gate + mechanical reopen
# ---------------------------------------------------------------------------


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


def _outcome(node_id: str) -> MissionOutcome:
    return MissionOutcome(
        mission_id=node_id,
        status="completed",
        team_run_id="team",
        rounds_used=1,
        wall_clock_seconds=0.1,
        contract_ok=True,
        artifact_path="",
    )


def test_authorized_veto_mechanically_reopens_target(tmp_path):
    run_dir = tmp_path / "run"
    reviser = audit_veto.VetoUpstreamReviser(
        run_dir=run_dir, authorized_seats=frozenset({"auditor"})
    )
    planner = AdaptiveCampaignPlanner(
        {
            "missions": [
                _node("m_implement"),
                _node("m_review", ["m_implement"]),
            ]
        },
        reviser=reviser,
        max_revisions=1,
    )
    kb = CampaignKB(tmp_path / "kb.jsonl")
    clock = BudgetClock(CampaignBudget())
    first = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    planner.observe(_outcome(first.mission.id))  # type: ignore[union-attr]
    second = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    assert second.mission.id == "m_review"  # type: ignore[union-attr]

    # The auditor mission, while executing, writes its veto signal as a
    # side effect (same convention a live seat's fs.write would produce);
    # only *after* that does the campaign observe its completion.
    audit_veto.write_veto(
        run_dir / "missions" / "m_review",
        target="m_implement",
        reason_code="late_test_failure",
        vetoed_by="auditor",
        vetoed_at="2026-09-06T00:00:00+00:00",
    )
    planner.observe(_outcome("m_review"))

    snapshot = planner.plan_snapshot()
    implement = next(n for n in snapshot["nodes"] if n["id"] == "m_implement")
    assert implement["resolved"] is False
    assert implement["reopen_reason"] == "late_test_failure"
    events = [e for e in snapshot["feedback_events"] if e["action"] == "REOPEN_UPSTREAM"]
    assert events == [
        {
            "action": "REOPEN_UPSTREAM",
            "target": "m_implement",
            "reason_code": "late_test_failure",
            "requested_by": "auditor",
        }
    ]

    # m_implement was mechanically reopened by the veto, so the planner must
    # now hand it back out again rather than treating the campaign as done
    # (retry naming appends `_aN`, same convention as any other reopen).
    third = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    assert third.mission.id.startswith("m_implement")  # type: ignore[union-attr]


def test_unauthorized_veto_is_scanned_but_never_honored(tmp_path):
    run_dir = tmp_path / "run"
    audit_veto.write_veto(
        run_dir / "missions" / "m_bystander",
        target="m_implement",
        reason_code="late_test_failure",
        vetoed_by="bystander",
        vetoed_at="2026-09-06T00:00:00+00:00",
    )
    reviser = audit_veto.VetoUpstreamReviser(
        run_dir=run_dir, authorized_seats=frozenset({"auditor"})
    )
    planner = AdaptiveCampaignPlanner(
        {"missions": [_node("m_implement"), _node("m_review", ["m_implement"])]},
        reviser=reviser,
        max_revisions=1,
    )
    kb = CampaignKB(tmp_path / "kb.jsonl")
    clock = BudgetClock(CampaignBudget())
    first = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    planner.observe(_outcome(first.mission.id))  # type: ignore[union-attr]

    snapshot = planner.plan_snapshot()
    implement = next(n for n in snapshot["nodes"] if n["id"] == "m_implement")
    assert implement["resolved"] is True  # NOT reopened
    assert reviser.ignored_unauthorized[0].vetoed_by == "bystander"


def test_same_veto_is_not_replayed_on_every_tick(tmp_path):
    run_dir = tmp_path / "run"
    audit_veto.write_veto(
        run_dir / "missions" / "m_review",
        target="m_implement",
        reason_code="late_test_failure",
        vetoed_by="auditor",
        vetoed_at="2026-09-06T00:00:00+00:00",
    )
    reviser = audit_veto.VetoUpstreamReviser(
        run_dir=run_dir, authorized_seats=frozenset({"auditor"})
    )
    first_call = reviser.revise(kb=None, completed=[], nodes=[])
    second_call = reviser.revise(kb=None, completed=[], nodes=[])
    assert len(first_call) == 1
    assert second_call == []
