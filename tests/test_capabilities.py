"""Capability declaration + mission preflight (thesis gate)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.capabilities import (
    RuntimeCapabilities,
    parse_requires_capabilities,
    preflight_mission,
    research_tools_needed,
)
from sciteam.mission import MissionBudget, MissionOutcome, MissionRunner, MissionSpec

LAB_ROOT = Path(__file__).resolve().parents[1]
SKILLS = LAB_ROOT / "assets" / "skills"


def test_parse_requires_section():
    md = """# Skill

## Requires capabilities
- literature.search
- literature.audit

## Steps
1. Go
"""
    assert parse_requires_capabilities(md) == ("literature.search", "literature.audit")


def test_parse_frontmatter_list():
    md = """---
requires_capabilities:
  - compute.eval
  - web.fetch
---

# Skill
"""
    assert parse_requires_capabilities(md) == ("compute.eval", "web.fetch")


def test_lit_survey_declares_research_caps():
    text = (SKILLS / "lit.survey" / "SKILL.md").read_text(encoding="utf-8")
    caps = parse_requires_capabilities(text)
    assert "literature.search" in caps
    assert research_tools_needed(caps)


def test_preflight_fails_when_capability_missing(tmp_path: Path):
    skill_dir = tmp_path / "needs_data"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "# x\n\n## Requires capabilities\n- dataset.materialize\n",
        encoding="utf-8",
    )
    # skills_dir layout: <root>/<skill_id>/SKILL.md
    root = tmp_path
    result = preflight_mission(
        skills_dir=root,
        allowlist=["needs_data"],
        available=RuntimeCapabilities.lab(dataset_materialize=False),
    )
    assert not result.ok
    assert any(i.capability == "dataset.materialize" for i in result.issues)


def test_preflight_ok_for_lit_survey_on_lab():
    result = preflight_mission(
        skills_dir=SKILLS,
        allowlist=["lit.survey", "protocol.register"],
        available=RuntimeCapabilities.lab(eval_runner=True),
    )
    assert result.ok
    assert "literature.search" in result.required


@pytest.mark.asyncio
async def test_mission_runner_blocks_before_ticks(tmp_path):
    """Missing capability → failed outcome, zero team work."""

    class BoomRuntime:
        async def run_subagent(self, *args, **kwargs):  # noqa: ANN002
            raise AssertionError("must not dispatch workers after failed preflight")

    from sciteam import ContractValidator, TeamOrchestrator, build_registry

    paradigms = build_registry(LAB_ROOT / "assets" / "teams" / "paradigms")
    runner = MissionRunner(
        orchestrator=TeamOrchestrator(runtime=BoomRuntime(), work_root=tmp_path / "work"),
        paradigms=paradigms,
        validator=ContractValidator(LAB_ROOT / "schemas"),
        artifacts_root=tmp_path / "missions",
        skills_dir=SKILLS,
        capabilities=RuntimeCapabilities(available=frozenset({"web.search"})),
    )
    spec = MissionSpec(
        id="m_block",
        kind="test",
        goal="should not run",
        paradigm="survey_gather",
        exit_contract="protocol_registration",
        skills_allowlist=("lit.survey",),
        budget=MissionBudget(),
    )
    outcome = await runner.run(spec, max_ticks=1)
    assert isinstance(outcome, MissionOutcome)
    assert outcome.status == "failed"
    assert "capability preflight" in outcome.detail
    assert outcome.team_run_id == ""
