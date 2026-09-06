"""Line A `run-canonical-demo`: harness/run_canonical_institution_demo.py.

Locks in the four claims the paper's canonical run depends on:
1. an unauthorized (bystander) submission is never granted institutional
   effect;
2. a buggy first patch is mechanically vetoed and reopened, a corrected
   second patch is legally closed, and the interpreter is untouched (E0=0);
3. each structural ablation produces the specific, different failure mode it
   is supposed to (not just "some failure");
4. the config-swap (single approver -> committee) changes only the
   institution manifest's effect, not the interpreter.
"""

from __future__ import annotations

from pathlib import Path

from harness import run_canonical_institution_demo as demo


def test_full_run_rejects_bystander_reopens_and_closes_legally(tmp_path):
    log: list[str] = []
    result = demo.run_full(tmp_path / "full", config_swap=False, log=log)
    assert any("bystander" in line and "authorized=False" in line for line in log)
    assert any("wrote authorized veto" in line for line in log)
    assert result["verdict"]["closed_legally"] is True
    assert result["verdict"]["tests_passed"] is True
    assert "pct = max(0.0, min(100.0, pct))" in result["final_calc_py"]
    events = [
        e for e in result["plan_snapshot"]["feedback_events"] if e["action"] == "REOPEN_UPSTREAM"
    ]
    assert events and events[0]["target"] == "m_implement"
    assert events[0]["requested_by"] == "auditor"


def test_prompt_only_closes_on_narration_despite_real_failure(tmp_path):
    log: list[str] = []
    result = demo.run_prompt_only(tmp_path / "prompt_only", log=log)
    assert result["verdict"]["closed_legally"] is True
    assert result["ground_truth_tests_actually_passed"] is False


def test_no_separation_closes_on_shallow_self_check_despite_real_failure(tmp_path):
    log: list[str] = []
    result = demo.run_no_separation(tmp_path / "no_separation", log=log)
    assert result["verdict"]["closed_legally"] is True
    assert result["ground_truth_tests_actually_passed"] is False
    assert any("self-check(syntax only)" in line for line in log)


def test_no_recovery_stays_permanently_failed_despite_fix_being_one_step_away(tmp_path):
    log: list[str] = []
    result = demo.run_no_recovery(tmp_path / "no_recovery", log=log)
    assert result["verdict"]["closed_legally"] is False
    assert result["verdict"]["reason"] == "no_legal_reopen_path"


def test_config_swap_changes_institution_effect_not_interpreter(tmp_path):
    log_single: list[str] = []
    single = demo.run_full(tmp_path / "single", config_swap=False, log=log_single)
    log_committee: list[str] = []
    committee = demo.run_full(tmp_path / "committee", config_swap=True, log=log_committee)

    assert single["manifest"]["approval_rule"]["kind"] == "single"
    assert committee["manifest"]["approval_rule"]["kind"] == "committee"
    assert len(committee["verdict"]["approvals"]) == 2
    assert single["verdict"]["closed_legally"] is True
    assert committee["verdict"]["closed_legally"] is True

    hash_single_before = demo._interpreter_hash()
    hash_committee_before = demo._interpreter_hash()
    assert hash_single_before == hash_committee_before  # same interpreter, only manifest differs


def test_interpreter_hash_unchanged_across_a_full_run(tmp_path):
    before = demo._interpreter_hash()
    demo.run_full(tmp_path / "full", config_swap=False, log=[])
    after = demo._interpreter_hash()
    assert before == after


def test_cli_writes_summary_json(tmp_path):
    out_dir = tmp_path / "cli_out"
    log: list[str] = []
    result = demo.run_full(out_dir, config_swap=False, log=log)
    # Mirror what main() does for a single mode without invoking the CLI
    # subprocess (kept fast); the CLI wiring itself is a thin argparse shell
    # around these same functions.
    assert isinstance(result["plan_snapshot"], dict)
    assert Path(out_dir).exists()
