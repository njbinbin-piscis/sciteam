"""Behavioral shadow (line A close-loop gap #1): sciteam/behavioral_shadow.py.

Uses hand-built synthetic paradigm fixtures (not the real asset tree) so this
test is a precise, non-flaky unit test of the authority predicate itself,
independent of how any particular real paradigm happens to be laid out.
"""

from __future__ import annotations

from pathlib import Path

from sciteam.behavioral_shadow import evaluate_authority_behavior, run_behavioral_shadow

_GOOD_PARADIGM = """\
id: sample_pipeline
name: Sample Pipeline
description: fixture
members:
- key: implementer
  agent_id: implementer
  role: implementer
  emits_exit_artifact: true
- key: auditor
  agent_id: auditor
  role: auditor
  authority:
  - may_assess_round
coordination:
  topology: chain
  stopping:
    kind: judgment
"""

# Authority collapse: no seat may ever submit.
_NO_STANDING_PARADIGM = _GOOD_PARADIGM.replace("  emits_exit_artifact: true\n", "")

# Privilege escalation: the auditor (assess seat) also gets exit-artifact duty.
_ESCALATED_PARADIGM = _GOOD_PARADIGM.replace(
    "  role: auditor\n  authority:\n  - may_assess_round\n",
    "  role: auditor\n  emits_exit_artifact: true\n  authority:\n  - may_assess_round\n",
)


def _write_paradigm(root: Path, text: str) -> Path:
    paradigms = root / "teams" / "paradigms"
    paradigms.mkdir(parents=True, exist_ok=True)
    (paradigms / "sample_pipeline.yaml").write_text(text, encoding="utf-8")
    return root


def test_good_paradigm_passes(tmp_path):
    root = _write_paradigm(tmp_path / "assets", _GOOD_PARADIGM)
    result = evaluate_authority_behavior(root)
    assert result["verdict"] == "pass"
    (para,) = result["paradigms"]
    assert para["ok"] is True
    assert para["granted"] == ["implementer"]
    assert para["wrongly_granted"] == []


def test_no_standing_seat_fails(tmp_path):
    root = _write_paradigm(tmp_path / "assets", _NO_STANDING_PARADIGM)
    result = evaluate_authority_behavior(root)
    assert result["verdict"] == "fail"
    (para,) = result["paradigms"]
    assert para["has_standing_seat"] is False


def test_assess_seat_declared_as_also_duty_fails_behind_structural_shadow(tmp_path):
    """`team_loader.load_team_config` already rejects a seat declared both
    `emits_exit_artifact` and `may_assess_round` (separation-of-powers at
    load time) — but that rejection is a `ValueError` from the *strict*
    loader, not from generic YAML/JSON parsing or `role_registry_preflight`
    (`scripts/run_shadow_eval.py::_check_role_registry` uses the latter).
    So this authority conflict is exactly the class of amendment the
    structural shadow alone would wave through and the behavioral shadow
    must catch."""
    root = _write_paradigm(tmp_path / "assets", _ESCALATED_PARADIGM)
    result = evaluate_authority_behavior(root)
    assert result["verdict"] == "fail"
    assert "error" in result


def test_no_paradigms_is_indeterminate_not_pass(tmp_path):
    root = tmp_path / "assets"
    (root / "teams" / "paradigms").mkdir(parents=True)
    result = evaluate_authority_behavior(root)
    assert result["verdict"] == "indeterminate"


def test_run_behavioral_shadow_flags_regression(tmp_path):
    incumbent = _write_paradigm(tmp_path / "incumbent", _GOOD_PARADIGM)
    amended = _write_paradigm(tmp_path / "amended", _NO_STANDING_PARADIGM)
    result = run_behavioral_shadow(incumbent, amended)
    assert result["incumbent"]["verdict"] == "pass"
    assert result["amended"]["verdict"] == "fail"
    assert result["verdict"] == "fail"
    assert result["regression"] is True


def test_run_behavioral_shadow_no_regression_when_already_broken(tmp_path):
    incumbent = _write_paradigm(tmp_path / "incumbent", _NO_STANDING_PARADIGM)
    amended = _write_paradigm(tmp_path / "amended", _NO_STANDING_PARADIGM)
    result = run_behavioral_shadow(incumbent, amended)
    assert result["verdict"] == "fail"
    assert result["regression"] is False
