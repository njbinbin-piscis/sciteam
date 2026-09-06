"""E0: ContractCoordinator must not contain scientific/completion decision ifs."""

from __future__ import annotations

import re

from tests.conftest import LAB_ROOT

COORD = LAB_ROOT / "sciteam" / "coordinator.py"
SCITEAM = LAB_ROOT / "sciteam"


def test_assess_round_has_no_stall_or_gate_decision_logic():
    text = COORD.read_text(encoding="utf-8")
    # Narrow to ContractCoordinator.assess_round body
    m = re.search(
        r"class ContractCoordinator[\s\S]*?async def assess_round\([\s\S]*?(?=\nclass |\Z)",
        text,
    )
    assert m, "ContractCoordinator.assess_round not found"
    body = m.group(0)
    assert "stagnation_max_rounds" not in body
    assert 'gate.get("equals")' not in body
    assert "gate.get('equals')" not in body


def test_sciteam_engine_has_no_tournament_role_stage_prompts():
    text = COORD.read_text(encoding="utf-8")
    assert "_ROLE_STAGE_PROMPTS" not in text
    assert "falsifiable candidate" not in text


def test_exit_artifact_has_no_role_hardcoded_draft_names():
    text = (SCITEAM / "exit_artifact.py").read_text(encoding="utf-8")
    assert "meta_review_draft" not in text
    assert "evolved_candidates" not in text
