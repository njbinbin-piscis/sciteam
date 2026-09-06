"""A4: mission-declared skills_allowlist unions with role/HR default_skills.

Before A4, ``mission.py`` treated a non-empty ``spec.skills_allowlist`` as a
full replacement for whatever HR staffed per role — the exact mechanism that
made the reviewer/critic/red_team role-registry gaps (A3) moot in practice,
because even a correctly-trained seat's skills never reached the worker once
the paradigm runner set a uniform per-contract allowlist. This module locks in
the union and its audit trail (``effective_skills.json`` next to
``staffing_report.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam import ContractValidator, MissionBudget, MissionRunner, MissionSpec, TeamOrchestrator
from sciteam.experiment_pack import recruit_agent
from sciteam.roster_policy import prefer_skills_for_roles
from sciteam.team_loader import build_registry

from tests.conftest import ArtifactWritingRuntime

LAB_ROOT = Path(__file__).resolve().parents[1]

VALID_LIT_MAP = {
    "entries": [
        {
            "citation_key": "k1",
            "title": "A Real Paper",
            "claims": ["a claim long enough"],
        }
    ]
}


@pytest.mark.asyncio
async def test_mission_declared_allowlist_unions_with_role_derived_skills(tmp_path):
    run_dir = tmp_path / "camp"
    run_dir.mkdir()
    # Pre-recruit with an explicit skill that is NOT in the mission-declared
    # allowlist below, mirroring HR differentiating a seat's training.
    recruit_agent(
        run_dir,
        agent_key="literature_lead",
        template_id="literature_lead",
        skills=["lit.survey", "lit.citation_guard", "evidence.claim_support"],
    )

    runner = MissionRunner(
        orchestrator=TeamOrchestrator(
            runtime=ArtifactWritingRuntime({"m_union": VALID_LIT_MAP}),
            work_root=tmp_path / "w",
        ),
        paradigms=build_registry(LAB_ROOT / "assets" / "teams" / "paradigms"),
        validator=ContractValidator(LAB_ROOT / "schemas"),
        artifacts_root=tmp_path / "m",
        pack_dir=run_dir / "pack",
    )
    outcome = await runner.run(
        MissionSpec(
            id="m_union",
            goal="x",
            paradigm="survey_gather",
            exit_contract="lit_map",
            # Deliberately disjoint from the recruited skills above.
            skills_allowlist=("plan.compose",),
            budget=MissionBudget(max_rounds=4),
        ),
        max_ticks=50,
    )
    assert outcome.status == "completed"

    session_dirs = sorted((run_dir / "pack" / "hr_sessions").glob("m_union_*"))
    assert session_dirs, "expected one HR session dir for m_union"
    effective_path = session_dirs[0] / "effective_skills.json"
    assert effective_path.is_file()
    record = json.loads(effective_path.read_text(encoding="utf-8"))
    assert record["mission_declared"] == ["plan.compose"]
    # The recruited literature_lead's own skills won the role-derived slot
    # (from_pack takes priority over catalog defaults).
    assert "lit.citation_guard" in record["role_derived"]
    # Union: both sides present, mission-declared first, no duplicates.
    assert record["effective_union"][0] == "plan.compose"
    assert "lit.citation_guard" in record["effective_union"]
    assert "lit.survey" in record["effective_union"]
    assert len(record["effective_union"]) == len(set(record["effective_union"]))


def test_prefer_skills_for_roles_used_when_pack_has_no_explicit_skills():
    # Sanity check on the fallback path the union relies on when no pack
    # agent carries its own `skills` — pure catalog lookup.
    skills = prefer_skills_for_roles(["reviewer"])
    assert "lit.citation_guard" in skills
    assert "review.epistemic_rigor" in skills
