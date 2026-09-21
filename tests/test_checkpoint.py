"""Resume must rebuild state without redoing acts.

The load-bearing assertion in this file is negative: after a crash, the
runner is **never** called again for a mission the ledger already records
as decided. Everything else — clock arithmetic, planner shape, chain
continuity — is in service of that one property, because re-running a
decided mission would duplicate an institutional effect and leave the
record describing something that did not happen.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from sciteam.campaign import Campaign, CampaignBudget, PlannerDecision
from sciteam.checkpoint import describe, read_resume_state, record_mission_decision
from sciteam.event_log import EventLog, verify_chain
from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome, MissionSpec
from sciteam.trace import CampaignTrace


def _spec(mission_id: str) -> MissionSpec:
    return MissionSpec(
        id=mission_id,
        goal=f"do {mission_id}",
        paradigm="pipeline_chain",
        exit_contract="results",
    )


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


class _ScriptedPlanner:
    """Proposes a fixed sequence, skipping whatever it has already observed."""

    def __init__(self, mission_ids: list[str]) -> None:
        self._ids = list(mission_ids)
        self.observed: list[str] = []

    def next_mission(self, *, last_outcome=None, kb=None, clock=None) -> PlannerDecision:
        for mission_id in self._ids:
            if mission_id not in self.observed:
                return PlannerDecision(action="run_mission", mission=_spec(mission_id))
        return PlannerDecision(action="stop", reason="all missions decided")

    def observe(self, outcome: MissionOutcome) -> None:
        self.observed.append(outcome.mission_id)


class _CountingRunner:
    def __init__(self, *, crash_after: int | None = None) -> None:
        self.ran: list[str] = []
        self._crash_after = crash_after

    async def run(self, spec: MissionSpec) -> MissionOutcome:
        if self._crash_after is not None and len(self.ran) >= self._crash_after:
            raise KeyboardInterrupt("simulated kill -9")
        self.ran.append(spec.id)
        return _outcome(spec.id)


def _campaign(tmp_path: Path, planner, runner, ledger: EventLog) -> Campaign:
    return Campaign(
        campaign_id="c1",
        goal="ship it",
        planner=planner,
        runner=runner,
        kb=CampaignKB(tmp_path / "kb.jsonl"),
        trace=CampaignTrace(tmp_path / "trace.jsonl"),
        budget=CampaignBudget(max_missions=10),
        ledger=ledger,
    )


@pytest.fixture
def interrupted(tmp_path: Path) -> tuple[Path, list[str]]:
    """A campaign of three missions, killed after the first two."""
    ledger = EventLog(tmp_path / "ledger.jsonl", run_id="c1")
    runner = _CountingRunner(crash_after=2)
    campaign = _campaign(tmp_path, _ScriptedPlanner(["m1", "m2", "m3"]), runner, ledger)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(campaign.run())
    assert runner.ran == ["m1", "m2"]
    return tmp_path / "ledger.jsonl", runner.ran


class TestResumeDoesNotRedoWork:
    def test_decided_missions_are_never_run_again(self, tmp_path, interrupted):
        path, before = interrupted
        runner = _CountingRunner()
        campaign = _campaign(tmp_path, _ScriptedPlanner(["m1", "m2", "m3"]), runner, EventLog(path))
        result = asyncio.run(campaign.run(resume=True))
        # The whole point: m1 and m2 are restored, only m3 executes.
        assert runner.ran == ["m3"]
        assert [o.mission_id for o in result.outcomes] == ["m1", "m2", "m3"]
        assert set(before) & set(runner.ran) == set()

    def test_the_planner_reaches_the_same_state_it_would_have(self, tmp_path, interrupted):
        path, _ = interrupted
        planner = _ScriptedPlanner(["m1", "m2", "m3"])
        campaign = _campaign(tmp_path, planner, _CountingRunner(), EventLog(path))
        asyncio.run(campaign.run(resume=True))
        assert planner.observed == ["m1", "m2", "m3"]

    def test_the_budget_is_not_spent_twice(self, tmp_path, interrupted):
        path, _ = interrupted
        campaign = _campaign(
            tmp_path, _ScriptedPlanner(["m1", "m2", "m3"]), _CountingRunner(), EventLog(path)
        )
        asyncio.run(campaign.run(resume=True))
        # Two restored + one executed, charged once each.
        assert campaign.clock.snapshot()["missions_run"] == 3

    def test_a_nondeterministic_planner_is_caught_not_tolerated(self, tmp_path, interrupted):
        path, _ = interrupted

        class Amnesiac(_ScriptedPlanner):
            def observe(self, outcome):  # forgets, so it re-proposes m1
                pass

        campaign = _campaign(
            tmp_path, Amnesiac(["m1", "m2", "m3"]), _CountingRunner(), EventLog(path)
        )
        with pytest.raises(RuntimeError, match="already recorded as decided"):
            asyncio.run(campaign.run(resume=True))

    def test_resume_without_a_ledger_is_refused(self, tmp_path):
        campaign = Campaign(
            campaign_id="c1",
            goal="g",
            planner=_ScriptedPlanner([]),
            runner=_CountingRunner(),
            kb=CampaignKB(tmp_path / "kb.jsonl"),
            trace=CampaignTrace(tmp_path / "trace.jsonl"),
        )
        with pytest.raises(ValueError, match="requires a ledger"):
            asyncio.run(campaign.run(resume=True))


class TestChainContinuity:
    def test_a_crash_and_restart_leave_one_verifiable_chain(self, tmp_path, interrupted):
        path, _ = interrupted
        campaign = _campaign(
            tmp_path, _ScriptedPlanner(["m1", "m2", "m3"]), _CountingRunner(), EventLog(path)
        )
        asyncio.run(campaign.run(resume=True))
        chain = verify_chain(path)
        assert chain.ok, chain.problems
        # One chain, not two partial ones.
        assert len({e.run_id for e in EventLog(path).events()}) == 1

    def test_the_resumption_is_itself_recorded(self, tmp_path, interrupted):
        path, _ = interrupted
        campaign = _campaign(
            tmp_path, _ScriptedPlanner(["m1", "m2", "m3"]), _CountingRunner(), EventLog(path)
        )
        asyncio.run(campaign.run(resume=True))
        starts = [e for e in EventLog(path).events() if e.kind == "run_started"]
        assert len(starts) == 2
        assert starts[0].payload["resumed_from"] is None
        assert starts[1].payload["resumed_from"] == ["m1", "m2"]

    def test_the_trace_distinguishes_a_resume_from_a_fresh_start(self, tmp_path, interrupted):
        path, _ = interrupted
        trace_path = tmp_path / "trace.jsonl"
        campaign = _campaign(
            tmp_path, _ScriptedPlanner(["m1", "m2", "m3"]), _CountingRunner(), EventLog(path)
        )
        asyncio.run(campaign.run(resume=True))
        events = CampaignTrace(trace_path).events()
        assert [e["event"] for e in events].count("campaign_resumed") == 1


class TestRealProcessKill:
    """The fixture above simulates a crash with an exception, which a
    `finally:` block could paper over. This one sends an actual SIGKILL, so
    nothing in the process gets a chance to tidy up — and then requires the
    resumed run to land field-for-field on the no-fault baseline."""

    DRIVER = """
import asyncio, sys, time
from pathlib import Path
from sciteam.campaign import Campaign, CampaignBudget, PlannerDecision
from sciteam.event_log import EventLog
from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome, MissionSpec
from sciteam.trace import CampaignTrace

OUT = Path(sys.argv[1]); MODE = sys.argv[2]
IDS = ["m1", "m2", "m3", "m4"]

class P:
    def __init__(self): self.seen = []
    def next_mission(self, *, last_outcome=None, kb=None, clock=None):
        for i in IDS:
            if i not in self.seen:
                return PlannerDecision(action="run_mission", mission=MissionSpec(
                    id=i, goal="g", paradigm="pipeline_chain", exit_contract="results"))
        return PlannerDecision(action="stop", reason="done")
    def observe(self, o): self.seen.append(o.mission_id)

class R:
    async def run(self, spec):
        with (OUT / "executed.log").open("a") as fh: fh.write(spec.id + "\\n")
        if spec.id == "m3" and MODE == "crash":
            (OUT / "ready").write_text("now")
            time.sleep(300)
        return MissionOutcome(mission_id=spec.id, status="completed", team_run_id="t",
                              rounds_used=1, wall_clock_seconds=0.1, contract_ok=True,
                              paradigm="pipeline_chain")

OUT.mkdir(parents=True, exist_ok=True)
ledger = EventLog(OUT / "ledger.jsonl") if MODE == "resume" else EventLog(
    OUT / "ledger.jsonl", run_id="k1")
c = Campaign(campaign_id="k1", goal="g", planner=P(), runner=R(),
             kb=CampaignKB(OUT / "kb.jsonl"), trace=CampaignTrace(OUT / "trace.jsonl"),
             budget=CampaignBudget(max_missions=20), ledger=ledger)
asyncio.run(c.run(resume=MODE == "resume"))
"""

    def _drive(self, tmp_path: Path, out: Path, mode: str):
        import subprocess

        script = tmp_path / "drive.py"
        script.write_text(self.DRIVER)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        return subprocess.Popen(
            [sys.executable, str(script), str(out), mode],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def test_sigkill_then_resume_matches_the_no_fault_baseline(self, tmp_path: Path):
        import signal
        import time

        crashed = tmp_path / "crashed"
        proc = self._drive(tmp_path, crashed, "crash")
        try:
            deadline = time.time() + 60
            while not (crashed / "ready").exists() and time.time() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"driver exited early: {proc.stderr.read().decode()[-2000:]}")
                time.sleep(0.05)
            assert (crashed / "ready").exists(), "driver never reached the kill point"
            proc.send_signal(signal.SIGKILL)
        finally:
            proc.wait(timeout=30)

        # m3 ran but was killed before its decision was recorded, so only
        # m1 and m2 are decided. The chain is still intact.
        assert verify_chain(crashed / "ledger.jsonl").ok
        assert read_resume_state(crashed / "ledger.jsonl").decided_mission_ids == ["m1", "m2"]

        (crashed / "executed.log").unlink()
        resumed = self._drive(tmp_path, crashed, "resume")
        assert resumed.wait(timeout=60) == 0, resumed.stderr.read().decode()[-2000:]

        # Only the undecided work re-ran.
        reran = (crashed / "executed.log").read_text().split()
        assert reran == ["m3", "m4"]

        baseline = tmp_path / "baseline"
        clean = self._drive(tmp_path, baseline, "clean")
        assert clean.wait(timeout=60) == 0, clean.stderr.read().decode()[-2000:]

        after_crash = read_resume_state(crashed / "ledger.jsonl")
        after_clean = read_resume_state(baseline / "ledger.jsonl")
        assert [o.to_dict() for o in after_crash.outcomes] == [
            o.to_dict() for o in after_clean.outcomes
        ]
        assert verify_chain(crashed / "ledger.jsonl").ok


class TestReadResumeState:
    def test_an_empty_ledger_has_nothing_to_restore(self, tmp_path):
        log = EventLog(tmp_path / "l.jsonl", run_id="c1")
        log.append("run_started")
        state = read_resume_state(log)
        assert not state
        assert state.outcomes == []

    def test_failed_missions_are_restored_too(self, tmp_path):
        log = EventLog(tmp_path / "l.jsonl", run_id="c1")
        record_mission_decision(log, _outcome("m1", succeeded=False))
        (restored,) = read_resume_state(log).outcomes
        # A rejection is a decision: re-running it would re-litigate a
        # question the institution already answered.
        assert restored.mission_id == "m1" and restored.succeeded is False
        assert restored.contract_errors == ["missing field"]

    def test_a_later_decision_supersedes_an_earlier_one(self, tmp_path):
        log = EventLog(tmp_path / "l.jsonl", run_id="c1")
        record_mission_decision(log, _outcome("m1", succeeded=False))
        record_mission_decision(log, _outcome("m2"))
        record_mission_decision(log, _outcome("m1", succeeded=True))
        outcomes = read_resume_state(log).outcomes
        # One entry per mission, in first-decision order, holding the
        # decision currently in force.
        assert [o.mission_id for o in outcomes] == ["m1", "m2"]
        assert outcomes[0].succeeded is True

    def test_non_decision_events_are_ignored(self, tmp_path):
        log = EventLog(tmp_path / "l.jsonl", run_id="c1")
        log.append("rule_version_set", rule_version="v1")
        log.append("x_plugin_note", payload={"subject": "m1"})
        log.append("audit_reported", effect="accepted", payload={"subject": "m1"})
        assert read_resume_state(log).outcomes == []

    def test_token_spend_is_carried_forward(self, tmp_path):
        log = EventLog(tmp_path / "l.jsonl", run_id="c1")
        log.append("llm_call", payload={"usage": {"total_tokens": 120}})
        log.append("llm_call", payload={"usage": {"total_tokens": 80}})
        assert read_resume_state(log).tokens_charged == 200

    def test_a_finished_run_says_so(self, tmp_path):
        log = EventLog(tmp_path / "l.jsonl", run_id="c1")
        record_mission_decision(log, _outcome("m1"))
        log.append("run_finished")
        state = read_resume_state(log)
        assert state.finished
        assert describe(state) == {
            "run_id": "c1",
            "finished": True,
            "decided_missions": ["m1"],
            "succeeded": 1,
            "tokens_charged": 0,
        }

    def test_reading_from_a_path_matches_reading_from_a_log(self, tmp_path):
        path = tmp_path / "l.jsonl"
        log = EventLog(path, run_id="c1")
        record_mission_decision(log, _outcome("m1"))
        assert read_resume_state(path).decided_mission_ids == (
            read_resume_state(log).decided_mission_ids
        )
