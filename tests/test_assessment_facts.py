"""assessment_facts is observation-only."""

from __future__ import annotations

import json

from sciteam.assessment_facts import build_assessment_facts
from sciteam.models import AgentSlot, Task, TaskState, TeamRun, TeamRunState


def test_facts_missing_artifact_no_decision(tmp_path):
    art = tmp_path / "out.json"
    run = TeamRun(
        id="r1",
        goal="g",
        team_profile_id="p",
        state=TeamRunState.RUNNING,
        active_round=1,
        agents=[AgentSlot(agent_key="writer", profile={"role": "writer"}, work_dir=str(tmp_path))],
        tasks=[
            Task(
                task_id="t1",
                agent_key="writer",
                prompt="p",
                state=TaskState.DONE,
                result="ok",
                round_index=1,
            )
        ],
        work_root=str(tmp_path),
        metadata={"artifact_path": str(art), "assess_roles": ["round_assessor"]},
    )
    facts = build_assessment_facts(run)
    assert facts["artifact_exists"] is False
    assert "decision" not in facts
    json.dumps(facts)  # round-trip


def test_facts_hash_when_present(tmp_path):
    art = tmp_path / "out.json"
    art.write_text('{"ok": true}', encoding="utf-8")
    run = TeamRun(
        id="r1",
        goal="g",
        team_profile_id="p",
        state=TeamRunState.RUNNING,
        active_round=1,
        agents=[],
        tasks=[],
        work_root=str(tmp_path),
        metadata={"artifact_path": str(art)},
    )
    facts = build_assessment_facts(run)
    assert facts["artifact_exists"] is True
    assert facts["artifact_sha256"]
    assert facts["artifact_size"] > 0
