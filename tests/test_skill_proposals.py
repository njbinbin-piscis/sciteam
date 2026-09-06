"""Skill revisions require real evidence and explicit pack approval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam.llm_worker import PromptAssets
from sciteam.skill_proposals import (
    approve_skill_proposal,
    build_evidence_bundle,
    create_skill_proposal,
)


def test_proposal_is_evidence_backed_and_does_not_mutate_assets(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log = run_dir / "agent_log.jsonl"
    log.write_text(
        json.dumps({"reason_code": "bad_step", "summary": "counterexample"}) + "\n",
        encoding="utf-8",
    )
    template = tmp_path / "assets" / "skills" / "demo" / "SKILL.md"
    template.parent.mkdir(parents=True)
    template.write_text("version: 1.0.0\nold\n", encoding="utf-8")
    before = template.read_text(encoding="utf-8")

    bundle = build_evidence_bundle(run_dir)
    assert bundle["events"][0]["ref"] == "agent_log.jsonl#L1"
    replacement = "---\nid: demo\nversion: 1.1.0\n---\n# Demo\nfixed\n"
    proposal = create_skill_proposal(
        run_dir=run_dir,
        skill_id="demo",
        proposed_version="1.1.0",
        scope="bad_step only",
        rationale="counterexample demonstrates missing guard",
        replacement_content=replacement,
        evidence_refs=["agent_log.jsonl#L1"],
    )
    assert template.read_text(encoding="utf-8") == before
    assert json.loads(proposal.read_text())["status"] == "awaiting_operator"

    pack = tmp_path / "experiment_pack"
    applied = approve_skill_proposal(proposal, experiment_pack_dir=pack, operator="reviewer")
    assert applied.read_text(encoding="utf-8") == replacement
    assert template.read_text(encoding="utf-8") == before

    assets = PromptAssets(
        prompts_dir=tmp_path / "prompts",
        skills_dir=tmp_path / "assets" / "skills",
        pack_dir=pack,
    )
    assets.skill_pack(["demo"])
    assert assets.loaded_skill_versions["demo"]["version"] == "1.1.0"
    assert str(pack) in assets.loaded_skill_versions["demo"]["source"]


def test_proposal_rejects_ghost_evidence(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="missing evidence"):
        create_skill_proposal(
            run_dir=run_dir,
            skill_id="demo",
            proposed_version="2.0.0",
            scope="global",
            rationale="unsupported",
            replacement_content="x",
            evidence_refs=["ghost.jsonl#L9"],
        )
