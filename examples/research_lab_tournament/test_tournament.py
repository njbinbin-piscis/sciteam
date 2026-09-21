"""Tests for Research Lab Hypothesis Tournament Reference Deployment."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sciteam.event_log import LEDGER_FILENAME, read_events, replay, verify_chain
from sciteam.mutation import mutate_file
from sciteam.team_loader import load_team_config

_run_path = Path(__file__).parent / "run.py"
_spec = importlib.util.spec_from_file_location("tournament_run", _run_path)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
run_research_tournament = _mod.run_research_tournament


def test_manifest_invariants() -> None:
    manifest_path = Path(__file__).parent / "institution.yaml"
    cfg = load_team_config(manifest_path)

    assert cfg.team_id == "research_lab_tournament"
    keys = {m.agent_key: m for m in cfg.members}

    assert "generation" in keys
    assert "reflection" in keys
    assert "ranking" in keys
    assert "proximity" in keys
    assert "meta_review" in keys
    assert "round_assessor" in keys

    # Separation of powers: generator cannot self-assess or close
    assert not keys["generation"].may_assess_round
    assert not keys["generation"].emits_exit_artifact

    # Meta-reviewer is the sole synthesis post permitted to emit the exit artifact
    assert keys["meta_review"].emits_exit_artifact


def test_run_tournament_workflow(tmp_path: Path) -> None:
    res = run_research_tournament(tmp_path)
    assert res["closed_legally"] is True
    assert res["lead_hypothesis"] == "H1"
    assert res["ledger_length"] >= 10

    # 1. Verify cryptographic hash chain
    chain = verify_chain(tmp_path / LEDGER_FILENAME)
    assert chain.ok is True

    # 2. Replay institutional state
    events = read_events(tmp_path / LEDGER_FILENAME)
    state = replay(events)

    assert state.finished is True

    # Falsifiability veto
    assert len(state.vetoes) == 1
    assert state.vetoes[0]["seat"] == "reflection"
    assert "unfalsifiable" in state.vetoes[0]["reason"]

    # Mechanical reopen
    assert len(state.reopenings) == 1
    assert state.reopenings[0]["seat"] == "planner"

    # Legal closure
    assert len(state.closed) == 1
    assert state.closed[0]["seat"] == "round_assessor"

    # 3. HTML view verification
    html = Path(res["view_html"]).read_text(encoding="utf-8")
    assert "CHAIN VERIFIED" in html
    assert "veto_issued" in html
    assert "http://" not in html and "https://" not in html


def test_tournament_manifest_mutation() -> None:
    manifest_path = Path(__file__).parent / "institution.yaml"
    report = mutate_file(manifest_path)
    assert report.score == 1.0, f"Mutation survivors: {[s.mutation for s in report.survivors]}"
