"""Tests for GitHub PR Review Institution Reference Deployment."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sciteam.event_log import LEDGER_FILENAME, read_events, replay, verify_chain
from sciteam.mutation import mutate_file
from sciteam.team_loader import load_team_config

_run_path = Path(__file__).parent / "run.py"
_spec = importlib.util.spec_from_file_location("github_pr_run", _run_path)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
run_pr_review_institution = _mod.run_pr_review_institution


def test_manifest_invariants() -> None:
    manifest_path = Path(__file__).parent / "institution.yaml"
    cfg = load_team_config(manifest_path)

    assert cfg.team_id == "github_pr_review"
    keys = {m.agent_key: m for m in cfg.members}
    assert "implementer" in keys
    assert "reviewer" in keys
    assert "red_team" in keys
    assert "assembler" in keys

    # Separation of powers: implementer holds no evaluation authority
    assert not keys["implementer"].may_assess_round
    assert not keys["implementer"].emits_exit_artifact

    # Release Assembler is the only seat that can seal the exit artifact
    assert keys["assembler"].emits_exit_artifact


def test_run_pr_review_workflow(tmp_path: Path) -> None:
    res = run_pr_review_institution(tmp_path)
    assert res["closed_legally"] is True
    assert res["ledger_length"] >= 10

    # 1. Verify ledger hash chain
    chain = verify_chain(tmp_path / LEDGER_FILENAME)
    assert chain.ok is True

    # 2. Replay institutional state
    events = read_events(tmp_path / LEDGER_FILENAME)
    state = replay(events)

    assert state.finished is True

    # Bystander rejection
    assert len(state.refused_acts) == 1
    assert state.refused_acts[0]["seat"] == "external_bot"
    assert state.refused_acts[0]["authority"] == "may_approve"

    # Authorized veto
    assert len(state.vetoes) == 1
    assert state.vetoes[0]["seat"] == "red_team"
    assert "hardcoded_secret" in state.vetoes[0]["reason"]

    # Mechanical reopen
    assert len(state.reopenings) == 1
    assert state.reopenings[0]["seat"] == "planner"

    # Legal closure
    assert len(state.closed) == 1
    assert state.closed[0]["seat"] == "assembler"

    # 3. HTML view verification: zero CDN dependencies
    html = Path(res["view_html"]).read_text(encoding="utf-8")
    assert "CHAIN VERIFIED" in html
    assert "veto_issued" in html
    assert "http://" not in html and "https://" not in html


def test_pr_review_manifest_mutation() -> None:
    manifest_path = Path(__file__).parent / "institution.yaml"
    report = mutate_file(manifest_path)
    assert report.score == 1.0, f"Mutation survivors: {[s.mutation for s in report.survivors]}"
