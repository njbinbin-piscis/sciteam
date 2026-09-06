"""Experiment pack + HR staffing."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.experiment_pack import (
    PackError,
    ensure_skills_in_pack,
    list_template_agents,
    list_template_skills,
    materialize_pack,
    pack_overview,
    read_pack_agent,
    recruit_agent,
    staff_roster_via_hr,
)


def test_materialize_and_staff(tmp_path: Path):
    run_dir = tmp_path / "camp_x"
    run_dir.mkdir()
    man = materialize_pack(run_dir, role_ids=["literature_lead", "evaluator"])
    assert "hr_officer" in man["agents"]
    assert (run_dir / "pack" / "agents" / "hr_officer" / "AGENT.md").is_file()
    assert (run_dir / "pack" / "skills" / "hr.recruit" / "SKILL.md").is_file()

    roster = staff_roster_via_hr(
        run_dir,
        [
            {"agent_key": "literature_lead", "name": "Literature Lead", "role": "literature_lead"},
            {"agent_key": "evaluator", "name": "Evaluator", "role": "evaluator"},
        ],
        mission_id="m_scout",
    )
    assert {a["agent_key"] for a in roster} == {"literature_lead", "evaluator"}
    assert all(a["staffed_by"] == "hr_officer" for a in roster)

    detail = read_pack_agent(run_dir, "literature_lead")
    assert detail["meta"]["instance"] is True
    assert "lit.survey" in (detail["meta"].get("skills") or []) or detail["skills"]

    ov = pack_overview(run_dir)
    assert ov["has_pack"]
    assert any(a["id"] == "hr_officer" for a in ov["agents"])


def test_templates_list_nonempty():
    skills = list_template_skills()
    agents = list_template_agents()
    assert any(s["id"] == "hr.recruit" for s in skills)
    assert any(a["id"] == "hr_officer" for a in agents)


def test_ensure_skills_in_pack_copies_allowlist(tmp_path: Path):
    run_dir = tmp_path / "camp_skills"
    run_dir.mkdir()
    materialize_pack(run_dir, role_ids=["hr_officer"])
    assert not (run_dir / "pack" / "skills" / "skill.revise" / "SKILL.md").is_file()
    copied = ensure_skills_in_pack(run_dir, ["skill.revise"])
    assert "skill.revise" in copied
    assert (run_dir / "pack" / "skills" / "skill.revise" / "SKILL.md").is_file()


def test_ensure_skills_in_pack_fails_closed_on_missing(tmp_path: Path):
    run_dir = tmp_path / "camp_missing"
    run_dir.mkdir()
    materialize_pack(run_dir, role_ids=["hr_officer"])
    with pytest.raises(PackError, match="not materialized"):
        ensure_skills_in_pack(run_dir, ["does.not.exist"])


def test_recruit_custom_specialty(tmp_path: Path):
    run_dir = tmp_path / "camp_y"
    run_dir.mkdir()
    materialize_pack(run_dir, role_ids=["hr_officer"])
    agent = recruit_agent(
        run_dir,
        agent_key="quantum_methods_lead",
        template_id="designer",
        train_notes="Specialize for variational ansatz design.",
        overwrite=True,
    )
    assert agent["id"] == "quantum_methods_lead"
    assert "Training notes" in agent["body"] or "variational" in agent["text"]
