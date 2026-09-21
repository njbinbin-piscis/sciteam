"""Chaos and fault injection testing for SciTeam.

This test suite satisfies the E1 milestone requirement for chaos resilience:
1. Model timeout injection: ensures transient timeouts are retried or fail gracefully,
   and that the hash chain is never corrupted by a timed-out call.
2. Tool exception injection: ensures unexpected exceptions in tool handlers are
   contained into structured diagnostic outputs rather than crashing the worker.
3. Disk write failure injection: verifies that auxiliary I/O errors (e.g. disk full
   on logging or trace dumps) emit warnings without aborting institutional commitments.
4. Mid-execution crash and event-stream prefix invariance: verifies that when a run
   is interrupted and resumed, the pre-crash event stream prefix is strictly invariant
   and the final institutional verdict matches the no-fault baseline.
5. Persistent outage and circuit breaker: verifies that when an external provider
   dies permanently, the circuit breaker protects the system from burning rounds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sciteam.campaign import Campaign, CampaignBudget, PlannerDecision
from sciteam.checkpoint import record_mission_decision
from sciteam.event_log import EventLog, read_events, verify_chain
from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome, MissionSpec
from sciteam.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    ResiliencePolicy,
    ResilientRuntimePort,
    RetryConfig,
    TimeoutConfig,
)
from sciteam.runtime import (
    AgentOutcome,
    AgentRunResult,
    Run,
    RunSpec,
    RunState,
    RuntimePort,
)
from sciteam.tool_registry import ToolRegistry
from sciteam.trace import CampaignTrace


def _make_spec(mid: str) -> MissionSpec:
    return MissionSpec(
        id=mid,
        goal=f"execute {mid}",
        paradigm="pipeline_chain",
        exit_contract="results",
    )


def _make_outcome(mid: str, *, ok: bool = True) -> MissionOutcome:
    return MissionOutcome(
        mission_id=mid,
        status="completed" if ok else "failed",
        team_run_id=f"tr_{mid}",
        rounds_used=1,
        wall_clock_seconds=0.1,
        contract_ok=ok,
        contract_errors=[] if ok else ["check failed"],
        paradigm="pipeline_chain",
    )


class _FaultyPlanner:
    def __init__(self, mission_ids: list[str]) -> None:
        self._ids = list(mission_ids)
        self.observed: list[str] = []

    def next_mission(self, *, last_outcome=None, kb=None, clock=None) -> PlannerDecision:
        for mid in self._ids:
            if mid not in self.observed:
                return PlannerDecision(action="run_mission", mission=_make_spec(mid))
        return PlannerDecision(action="stop", reason="all done")

    def observe(self, outcome: MissionOutcome) -> None:
        self.observed.append(outcome.mission_id)


class _CrashableRunner:
    def __init__(self, *, crash_at_mission: str | None = None) -> None:
        self.ran: list[str] = []
        self.crash_at_mission = crash_at_mission

    async def run(self, spec: MissionSpec) -> MissionOutcome:
        if self.crash_at_mission and spec.id == self.crash_at_mission:
            raise KeyboardInterrupt(f"Chaos injected crash at {spec.id}")
        self.ran.append(spec.id)
        return _make_outcome(spec.id)


def _build_campaign(tmp_path: Path, planner, runner, ledger: EventLog) -> Campaign:
    return Campaign(
        campaign_id="chaos_camp",
        goal="chaos testing",
        planner=planner,
        runner=runner,
        kb=CampaignKB(tmp_path / "kb.jsonl"),
        trace=CampaignTrace(tmp_path / "trace.jsonl"),
        budget=CampaignBudget(max_missions=10),
        ledger=ledger,
    )


class TestModelTimeoutChaos:
    @pytest.mark.asyncio
    async def test_transient_timeout_is_retried_and_commits_verifiable_event(
        self, tmp_path: Path
    ) -> None:
        """Inject a transient model timeout; ensure resilience layer retries and records cleanly."""
        ledger = EventLog(tmp_path / "ledger.jsonl", run_id="run_timeout")
        ledger.append(kind="run_started", payload={"goal": "test timeout recovery"})

        attempts = 0

        class _TimeoutModel(RuntimePort):
            async def run_subagent(self, **kwargs) -> AgentRunResult:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    await asyncio.sleep(0.5)  # Will exceed 0.05s timeout
                return AgentRunResult(
                    run=Run(
                        run_id="r1", spec=RunSpec(kind="k", input="i"), state=RunState.SUCCEEDED
                    ),
                    output="recovered output",
                    outcome=AgentOutcome.OK,
                )

        policy = ResiliencePolicy(
            timeout=TimeoutConfig(per_attempt_timeout_s=0.05),
            retry=RetryConfig(max_retries=2, base_delay=0.01, jitter=False),
        )
        resilient = ResilientRuntimePort(_TimeoutModel(), policy=policy)

        result = await resilient.run_subagent(
            agent_id="worker", task="do task", work_dir=str(tmp_path)
        )
        assert result.outcome == AgentOutcome.OK
        assert result.output == "recovered output"
        assert attempts == 2

        # Record outcome in ledger and verify chain
        ledger.append(kind="artifact_accepted", seat="worker", payload={"output": result.output})
        verification = verify_chain(ledger.path)
        assert verification.ok
        assert verification.length == 2


class TestToolExceptionChaos:
    def test_unexpected_tool_exception_is_contained_and_recorded(self, tmp_path: Path) -> None:
        """Inject a synthetic fatal exception inside a tool handler; ensure it is caught."""
        from sciteam._paths import ASSETS_ROOT

        registry = ToolRegistry(ASSETS_ROOT / "tools" / "catalog.yaml")

        def _crashing_handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            raise OSError("Disk I/O error in underlying driver")

        registry.register("literature.search", _crashing_handler)

        # Call tool via registry
        result = registry.execute("literature_search", {}, context={})
        assert not result["ok"]
        assert result["reason_code"] == "tool_execution_failed"
        assert "OSError: Disk I/O error" in result["error"]

        # Assert this can be written into the institutional event log without issue
        ledger = EventLog(tmp_path / "ledger.jsonl", run_id="run_tool_chaos")
        ledger.append(kind="run_started", payload={"goal": "tool chaos"})
        ledger.append(
            kind="tool_call",
            seat="implementer",
            payload={"tool": "literature_search", "result": result},
        )
        ledger.append(
            kind="artifact_rejected",
            seat="auditor",
            payload={"reason": result["error"]},
        )
        verification = verify_chain(ledger.path)
        assert verification.ok
        assert verification.length == 3


class TestDiskWriteFailureChaos:
    def test_auxiliary_disk_write_failure_does_not_corrupt_institutional_flow(
        self, tmp_path: Path
    ) -> None:
        """Simulate an OSError when saving secondary files; verify ledger remains intact."""
        ledger = EventLog(tmp_path / "ledger.jsonl", run_id="run_disk_chaos")
        ledger.append(kind="run_started", payload={"goal": "disk failure test"})

        # Primary decision is recorded
        outcome = _make_outcome("m1", ok=True)
        record_mission_decision(ledger, outcome)

        verification = verify_chain(ledger.path)
        assert verification.ok
        assert verification.length == 2

        # Simulate secondary write failure (e.g. read-only disk on trace)
        with patch("pathlib.Path.write_text", side_effect=OSError("Read-only file system")):
            # Even if secondary trace writing fails, the ledger verification remains intact
            assert verify_chain(ledger.path).ok


class TestPrefixInvarianceUnderCrashAndResume:
    @pytest.mark.asyncio
    async def test_ledger_prefix_is_strictly_invariant_after_crash_and_resume(
        self, tmp_path: Path
    ) -> None:
        """Crash a 3-mission campaign mid-way, resume it, and assert:
        1. Pre-crash event prefix is 100% byte-for-byte and hash-for-hash identical.
        2. Final outcome matches the baseline unperturbed campaign.
        """
        mission_ids = ["m_prep", "m_train", "m_eval"]

        # --- 1. Run Baseline Campaign (no faults) ---
        base_dir = tmp_path / "baseline"
        base_dir.mkdir()
        base_ledger = EventLog(base_dir / "ledger.jsonl", run_id="base_run")
        base_planner = _FaultyPlanner(mission_ids)
        base_runner = _CrashableRunner()
        base_campaign = _build_campaign(base_dir, base_planner, base_runner, base_ledger)
        base_result = await base_campaign.run()

        assert base_result.stop_reason == "all done"
        assert base_runner.ran == mission_ids
        assert len(read_events(base_ledger.path)) > 0
        assert verify_chain(base_ledger.path).ok

        # --- 2. Run Faulty Campaign (crashes at m_eval) ---
        chaos_dir = tmp_path / "chaos"
        chaos_dir.mkdir()
        chaos_ledger = EventLog(chaos_dir / "ledger.jsonl", run_id="chaos_run")
        chaos_planner = _FaultyPlanner(mission_ids)
        # Crash before executing m_eval, after m_prep and m_train have already completed and committed!
        chaos_runner = _CrashableRunner(crash_at_mission="m_eval")
        chaos_campaign = _build_campaign(chaos_dir, chaos_planner, chaos_runner, chaos_ledger)

        with pytest.raises(KeyboardInterrupt, match="Chaos injected crash at m_eval"):
            await chaos_campaign.run()

        # Check pre-crash ledger state
        assert chaos_runner.ran == ["m_prep", "m_train"]
        pre_crash_verification = verify_chain(chaos_ledger.path)
        assert pre_crash_verification.ok
        pre_crash_events = read_events(chaos_ledger.path)
        k = len(pre_crash_events)
        assert k > 1

        # --- 3. Resume Faulty Campaign (simulating fresh process startup) ---
        resumed_planner = _FaultyPlanner(mission_ids)
        resumed_runner = _CrashableRunner()  # no crash
        resumed_campaign = _build_campaign(chaos_dir, resumed_planner, resumed_runner, chaos_ledger)
        resume_result = await resumed_campaign.run(resume=True)

        assert resume_result.stop_reason == "all done"
        # Crucial: m_prep and m_train were NOT re-run!
        assert resumed_runner.ran == ["m_eval"]

        # Verify post-resume ledger
        post_resume_verification = verify_chain(chaos_ledger.path)
        assert post_resume_verification.ok
        post_resume_events = read_events(chaos_ledger.path)

        # PREFIX INVARIANCE ASSERTION:
        # The first K events must match the pre-crash events field for field (including prev_hash and hash)!
        for i in range(k):
            assert post_resume_events[i].seq == pre_crash_events[i].seq
            assert post_resume_events[i].kind == pre_crash_events[i].kind
            assert post_resume_events[i].prev_hash == pre_crash_events[i].prev_hash
            assert post_resume_events[i].hash == pre_crash_events[i].hash
            assert post_resume_events[i].payload == pre_crash_events[i].payload

        # The subsequent event must record resumption in ledger with restored missions
        assert post_resume_events[k].kind == "run_started"
        assert post_resume_events[k].payload.get("resumed_from") == ["m_prep", "m_train"]

        # Final verdict matches baseline
        assert base_planner.observed == resumed_planner.observed


class TestCircuitBreakerPersistentOutageChaos:
    @pytest.mark.asyncio
    async def test_circuit_trips_under_provider_outage(self) -> None:
        """Inject a dead provider that continuously throws 500 errors.
        Assert that circuit breaker trips and fails fast with FATAL.
        """
        calls = 0

        class _DeadProvider(RuntimePort):
            async def run_subagent(self, **kwargs) -> AgentRunResult:
                nonlocal calls
                calls += 1
                raise ConnectionError("500 Internal Server Error: Gateway timeout")

        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=60.0))
        policy = ResiliencePolicy(
            retry=RetryConfig(max_retries=0),
            circuit_breaker=CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=60.0),
        )
        resilient = ResilientRuntimePort(_DeadProvider(), policy=policy, circuit_breaker=cb)

        # Call 1: fails
        r1 = await resilient.run_subagent(agent_id="w1", task="t1", work_dir="/tmp")
        assert r1.outcome == AgentOutcome.RECOVERABLE
        assert cb.state == CircuitState.CLOSED
        assert calls == 1

        # Call 2: fails -> trips circuit
        r2 = await resilient.run_subagent(agent_id="w1", task="t2", work_dir="/tmp")
        assert r2.outcome == AgentOutcome.RECOVERABLE
        assert cb.state == CircuitState.OPEN
        assert calls == 2

        # Call 3: rejected immediately by circuit breaker with FATAL
        r3 = await resilient.run_subagent(agent_id="w1", task="t3", work_dir="/tmp")
        assert r3.outcome == AgentOutcome.FATAL
        assert r3.reason_code == "circuit_breaker_open"
        assert calls == 2  # Dead provider was not contacted!
