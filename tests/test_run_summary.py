"""docs/28 §7.2 (E7) — end-of-run health/cost/resilience summary."""

from __future__ import annotations

import json

from sciteam.checkpoint import record_mission_decision
from sciteam.event_log import EventLog
from sciteam.mission_contracts import MissionOutcome
from sciteam.run_summary import NullObserver, build_run_summary, write_run_summary


def _outcome(mission_id: str, *, succeeded: bool = True) -> MissionOutcome:
    return MissionOutcome(
        mission_id=mission_id,
        status="completed" if succeeded else "contract_failed",
        team_run_id=f"tr_{mission_id}",
        rounds_used=2,
        wall_clock_seconds=0.5,
        contract_ok=succeeded,
        contract_errors=[] if succeeded else ["missing field"],
        paradigm="pipeline_chain",
    )


def test_build_run_summary_from_ledger(tmp_path):
    log = EventLog(tmp_path / "l.jsonl", run_id="run-1")
    record_mission_decision(log, _outcome("m1", succeeded=True))
    record_mission_decision(log, _outcome("m2", succeeded=False))
    log.append(
        "llm_call",
        seat="chair",
        payload={
            "model": "m",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "usd": 0.5,
        },
    )
    log.append("run_finished")

    summary = build_run_summary(log)
    assert summary.run_id == "run-1"
    assert summary.finished is True
    assert summary.missions_decided == 2
    assert summary.missions_succeeded == 1
    assert summary.missions_failed == 1
    assert summary.total_usd == 0.5
    assert summary.cost_by_seat[0]["seat"] == "chair"
    assert summary.resilience == {}


def test_build_run_summary_includes_resilience_metrics_when_available(tmp_path):
    log = EventLog(tmp_path / "l.jsonl", run_id="run-2")
    record_mission_decision(log, _outcome("m1"))

    class _FakeResilientRuntime:
        def metrics(self):
            return {"total_calls": 3, "circuit_state": "closed"}

    summary = build_run_summary(log, resilient_runtime=_FakeResilientRuntime())
    assert summary.resilience == {"total_calls": 3, "circuit_state": "closed"}


def test_write_run_summary_writes_json_and_notifies_observer(tmp_path):
    log = EventLog(tmp_path / "l.jsonl", run_id="run-3")
    record_mission_decision(log, _outcome("m1"))
    summary = build_run_summary(log)

    seen = []

    class _Observer:
        def observe_run_summary(self, payload):
            seen.append(payload)

    out_path = tmp_path / "summary.json"
    write_run_summary(out_path, summary, observer=_Observer())
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["run_id"] == "run-3"
    assert seen and seen[0]["run_id"] == "run-3"


def test_null_observer_is_a_no_op():
    NullObserver().observe_run_summary({"anything": True})
