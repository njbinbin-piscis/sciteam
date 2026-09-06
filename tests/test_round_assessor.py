"""ScriptedAssessor + envelope parsing (paradigm-agnostic)."""

from __future__ import annotations

import asyncio

from sciteam.models import AssessmentDecision, TeamRun, TeamRunState
from sciteam.round_assessor import ScriptedAssessor, parse_assessor_envelope


def _run() -> TeamRun:
    return TeamRun(
        id="r",
        goal="g",
        team_profile_id="p",
        state=TeamRunState.RUNNING,
        active_round=1,
        agents=[],
        tasks=[],
        work_root="/tmp",
    )


def test_need_artifact():
    facts = {
        "artifact_exists": False,
        "active_round": 1,
        "charter_stagnation_max_rounds": 5,
        "assess_roles": ["round_assessor"],
        "artifact_emit_roles": ["writer"],
        "wave_tasks": [{"agent_key": "writer", "state": "done"}],
    }
    raw = asyncio.run(ScriptedAssessor().assess(facts, _run()))
    assert raw["decision"] == "continue"
    assert raw["reason_code"] == "need_artifact"


def test_exit_stalled():
    facts = {
        "artifact_exists": False,
        "active_round": 3,
        "charter_stagnation_max_rounds": 2,
        "assess_roles": ["round_assessor"],
        "wave_tasks": [{"agent_key": "writer", "state": "done"}],
    }
    raw = asyncio.run(ScriptedAssessor().assess(facts, _run()))
    assert raw["decision"] == "failed"
    assert raw["reason_code"] == "exit_stalled"


def test_ok_complete():
    facts = {
        "artifact_exists": True,
        "active_round": 1,
        "assess_roles": ["round_assessor"],
        "wave_tasks": [{"agent_key": "writer", "state": "done"}],
        "artifact_gate": None,
    }
    raw = asyncio.run(ScriptedAssessor().assess(facts, _run()))
    assert raw["decision"] == "completed"
    assert raw["reason_code"] == "ok_complete"


def test_gate_unsatisfied():
    facts = {
        "artifact_exists": True,
        "active_round": 1,
        "assess_roles": ["round_assessor"],
        "artifact_gate": {"pointer": "/success", "equals": True},
        "gate_observed_value": False,
        "wave_tasks": [{"agent_key": "writer", "state": "done"}],
    }
    raw = asyncio.run(ScriptedAssessor().assess(facts, _run()))
    assert raw["decision"] == "continue"
    assert raw["reason_code"] == "gate_unsatisfied"


def test_illegal_envelope():
    """Illegal shape is recoverable CONTINUE, not mission-killing FAILED."""
    verdict = parse_assessor_envelope({"decision": "completed", "reason_code": "nope"})
    assert verdict.decision == AssessmentDecision.CONTINUE
    assert verdict.reason_code == "illegal_assessment"
    assert "Re-emit" in (verdict.next_round_hint or "")
