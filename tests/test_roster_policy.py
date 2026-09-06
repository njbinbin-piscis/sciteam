"""Roster professionalism policy tests."""

from __future__ import annotations

import pytest
from sciteam.roster_policy import (
    is_banned_identity,
    load_role_catalog,
    prefer_skills_for_roles,
    role_registry_preflight,
    validate_roster,
)
from sciteam.team_loader import build_registry


def test_bans_stage_and_worker_names():
    assert is_banned_identity("stage_a")
    assert is_banned_identity("stage-B")
    assert is_banned_identity("worker")
    assert is_banned_identity("generic")
    assert not is_banned_identity("literature_lead")
    assert not is_banned_identity("assembler")


def test_validate_roster_rejects_generic():
    issues = validate_roster([{"agent_key": "stage_a", "agent_id": "worker", "name": "Stage A"}])
    assert issues
    assert any("stage_a" in i.format() for i in issues)


def test_paradigm_rosters_are_professional():
    registry = build_registry(
        __file__.replace("tests/test_roster_policy.py", "assets/teams/paradigms")
    )
    # build_registry loads and validates; pipeline_chain must not use stage_*
    pc = registry.get("pipeline_chain")
    assert pc is not None
    keys = {m.agent_key for m in pc.members}
    assert {"analyst", "designer", "assembler", "round_assessor"} <= keys
    assert "stage_a" not in keys
    assert "round_assessor" in pc.assess_roles()


def test_role_catalog_skills_union():
    cat = load_role_catalog()
    assert "literature_lead" in cat
    skills = prefer_skills_for_roles(["literature_lead", "evaluator"], catalog=cat)
    assert "lit.survey" in skills
    assert "eval.run_frozen" in skills


def test_role_registry_preflight_passes_for_registered_role(tmp_path):
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parents[1] / "assets" / "prompts"
    issues = role_registry_preflight(
        [{"agent_key": "reviewer", "role": "reviewer"}],
        prompts_dir=prompts_dir,
    )
    assert issues == []


def test_role_registry_preflight_flags_unregistered_role(tmp_path):
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parents[1] / "assets" / "prompts"
    issues = role_registry_preflight(
        [{"agent_key": "made_up_specialist", "role": "made_up_specialist"}],
        prompts_dir=prompts_dir,
    )
    assert len(issues) == 1
    detail = issues[0].format()
    assert "made_up_specialist" in detail
    assert "CATALOG.yaml" in detail
    assert "worker.md" in detail


def test_role_registry_preflight_flags_prompt_only_gap(tmp_path):
    """A role with a catalog entry but no dedicated prompt (agent_key mismatch,
    e.g. `watcher` seat / `analyst` role before the watcher.md fix) must still
    fail — catalog skills alone do not give the seat role-specific duty text.
    """
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parents[1] / "assets" / "prompts"
    issues = role_registry_preflight(
        [{"agent_key": "no_such_prompt_stem", "role": "analyst"}],
        prompts_dir=prompts_dir,
    )
    assert len(issues) == 1
    assert "no_such_prompt_stem.md" in issues[0].format()


def test_role_registry_preflight_ignores_relax_env(monkeypatch):
    """Unlike validate_roster, this check is not gated by SCITEAM_ROSTER_RELAX —
    it protects against fail-open training gaps, not naming style."""
    from pathlib import Path

    monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
    prompts_dir = Path(__file__).resolve().parents[1] / "assets" / "prompts"
    issues = role_registry_preflight(
        [{"agent_key": "made_up_specialist", "role": "made_up_specialist"}],
        prompts_dir=prompts_dir,
    )
    assert issues


@pytest.mark.asyncio
async def test_mission_rejects_banned_roster(tmp_path):
    from pathlib import Path

    from sciteam import (
        ContractValidator,
        MissionBudget,
        MissionRunner,
        MissionSpec,
        TeamOrchestrator,
    )
    from sciteam.team_loader import build_registry

    lab = Path(__file__).resolve().parents[1]

    class Boom:
        async def run_subagent(self, *a, **k):  # noqa: ANN002
            raise AssertionError("should not run")

    runner = MissionRunner(
        orchestrator=TeamOrchestrator(runtime=Boom(), work_root=tmp_path / "w"),
        paradigms=build_registry(lab / "assets" / "teams" / "paradigms"),
        validator=ContractValidator(lab / "schemas"),
        artifacts_root=tmp_path / "m",
    )
    outcome = await runner.run(
        MissionSpec(
            id="m_bad_roster",
            goal="x",
            paradigm="survey_gather",
            exit_contract="protocol_registration",
            roster=[{"agent_key": "worker", "agent_id": "worker", "name": "Worker"}],
            budget=MissionBudget(),
        ),
        max_ticks=1,
    )
    assert outcome.status == "failed"
    assert "roster policy" in outcome.detail


@pytest.mark.asyncio
async def test_mission_rejects_unregistered_role_when_prompts_dir_set(tmp_path):
    from pathlib import Path

    from sciteam import (
        ContractValidator,
        MissionBudget,
        MissionRunner,
        MissionSpec,
        TeamOrchestrator,
    )
    from sciteam.team_loader import build_registry

    lab = Path(__file__).resolve().parents[1]

    class Boom:
        async def run_subagent(self, *a, **k):  # noqa: ANN002
            raise AssertionError("should not run")

    runner = MissionRunner(
        orchestrator=TeamOrchestrator(runtime=Boom(), work_root=tmp_path / "w"),
        paradigms=build_registry(lab / "assets" / "teams" / "paradigms"),
        validator=ContractValidator(lab / "schemas"),
        artifacts_root=tmp_path / "m",
        prompts_dir=lab / "assets" / "prompts",
    )
    outcome = await runner.run(
        MissionSpec(
            id="m_no_role",
            goal="x",
            paradigm="survey_gather",
            exit_contract="protocol_registration",
            roster=[
                {
                    "agent_key": "totally_unregistered_specialist",
                    "agent_id": "totally_unregistered_specialist",
                    "role": "totally_unregistered_specialist",
                    "name": "Unregistered Specialist",
                }
            ],
            budget=MissionBudget(),
        ),
        max_ticks=1,
    )
    assert outcome.status == "failed"
    assert "role registry preflight" in outcome.detail
