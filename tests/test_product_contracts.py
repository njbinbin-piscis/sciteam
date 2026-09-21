"""docs/28 §7.2 (E7) — remaining product contract suite.

Complements what is already covered elsewhere:
* SIGKILL / crash+resume prefix invariance -> `tests/test_chaos.py`,
  `tests/test_session.py::test_sigkill_mid_write_is_recoverable`.
* Disk write failures, model timeout, persistent-outage circuit breaker
  -> `tests/test_chaos.py`.
* `context_overflow` classification + one compaction-retry, non-shrinking
  rejection -> `tests/test_compaction_port.py`.
* Sandbox unavailable (unattended profile without Docker) ->
  `tests/test_sandbox_profile.py`.
* Cold-start Team/Mission recovery -> `tests/test_recovery.py`.

This file adds the contracts none of the above exercise: a provider 429/
rate-limit that is not one of `RetryConfig.retryable_exceptions`'s built-in
types still gets retried; cancelling a `Session.run()` call mid-flight
leaves a verifiable, resumable session log; resubmitting an identical
mission decision does not create a second institutional effect; and a
500-round soak (with periodic simulated crash + cold restart) leaves no
corruption and no unbounded growth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sciteam.checkpoint import read_resume_state, record_mission_decision
from sciteam.event_log import EventLog, verify_chain
from sciteam.mission_contracts import MissionOutcome
from sciteam.models import TeamRunState, WorkItem, WorkItemState
from sciteam.orchestrator import CreateSpec, TeamOrchestrator
from sciteam.persist import snapshot_team_run
from sciteam.resilience import ResiliencePolicy, ResilientRuntimePort, RetryConfig
from sciteam.runtime import AgentOutcome, AgentRunResult, Run, RunSpec, RunState, RuntimePort
from sciteam.session_sdk import Session


class TestProviderRateLimitChaos:
    """A provider raising a vendor-specific 429 exception type this
    package has never seen must still be retried — see
    `sciteam.resilience._RESILIENCE_RETRYABLE_ERROR_CODES`."""

    @pytest.mark.asyncio
    async def test_unrecognized_429_exception_type_is_retried(self, tmp_path: Path) -> None:
        class _VendorRateLimitError(Exception):
            """Stands in for e.g. `openai.RateLimitError` — not one of
            `RetryConfig.retryable_exceptions`'s built-in types."""

        attempts = 0

        class _ThrottledModel(RuntimePort):
            async def run_subagent(self, **kwargs) -> AgentRunResult:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise _VendorRateLimitError("429 Too Many Requests: slow down")
                return AgentRunResult(
                    run=Run(
                        run_id="r1", spec=RunSpec(kind="k", input="i"), state=RunState.SUCCEEDED
                    ),
                    output="recovered",
                    outcome=AgentOutcome.OK,
                )

        resilient = ResilientRuntimePort(
            _ThrottledModel(),
            policy=ResiliencePolicy(
                retry=RetryConfig(max_retries=1, base_delay=0.01, jitter=False)
            ),
        )
        result = await resilient.run_subagent(agent_id="a", task="t", work_dir=str(tmp_path))
        assert result.outcome == AgentOutcome.OK
        assert result.output == "recovered"
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_a_truly_unknown_fatal_error_is_not_retried(self, tmp_path: Path) -> None:
        """The 429/5xx carve-out must not become a blanket always-retry —
        an unrelated programming error stays FATAL on the first attempt."""

        class _ProgrammingBug(Exception):
            pass

        attempts = 0

        class _BuggyModel(RuntimePort):
            async def run_subagent(self, **kwargs) -> AgentRunResult:
                nonlocal attempts
                attempts += 1
                raise _ProgrammingBug("unexpected keyword argument")

        resilient = ResilientRuntimePort(_BuggyModel())
        result = await resilient.run_subagent(agent_id="a", task="t", work_dir=str(tmp_path))
        assert result.outcome == AgentOutcome.FATAL
        assert attempts == 1


class TestSessionCancellationContract:
    @pytest.mark.asyncio
    async def test_cancelling_a_run_mid_flight_leaves_a_resumable_session(
        self, tmp_path: Path
    ) -> None:
        class _HangingProvider:
            async def acomplete(self, messages, *, tools=None, tool_choice=None):
                await asyncio.sleep(30)
                raise AssertionError("should have been cancelled before this returns")

        class _EchoProvider:
            async def acomplete(self, messages, *, tools=None, tool_choice=None):
                last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

                class _Resp:
                    content = f"echo: {last}"
                    tool_calls = None

                return _Resp()

        session = Session.create(
            workspace=tmp_path, session_id="cancel-me", provider=_HangingProvider()
        )
        task = asyncio.ensure_future(session.run("please hang forever"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.close()

        # The `user_message` event that preceded the hang must be intact —
        # cancellation lands on an `await` inside the provider call, after
        # that synchronous append already completed.
        kinds = [e.kind for e in session.events()]
        assert kinds == ["user_message"]

        resumed = Session.resume(
            workspace=tmp_path, session_id="cancel-me", provider=_EchoProvider()
        )
        turn = await resumed.run("try again")
        assert turn.content == "echo: try again"
        # No replayed/duplicated user_message from the cancelled attempt.
        resumed_kinds = [e.kind for e in resumed.events()]
        assert resumed_kinds.count("user_message") == 2


class TestDuplicateSubmissionIdempotency:
    def test_resubmitting_an_identical_decision_does_not_double_the_effect(
        self, tmp_path: Path
    ) -> None:
        ledger = EventLog(tmp_path / "l.jsonl", run_id="run-dup")
        outcome = MissionOutcome(
            mission_id="m1",
            status="completed",
            team_run_id="tr_m1",
            rounds_used=2,
            wall_clock_seconds=0.5,
            contract_ok=True,
            contract_errors=[],
            paradigm="pipeline_chain",
        )
        record_mission_decision(ledger, outcome)
        # A retried/duplicated submit call (e.g. a client retry after a
        # dropped ack) appends a second raw ledger entry...
        record_mission_decision(ledger, outcome)
        assert ledger.length == 2

        # ...but the institutional effect — "how many missions has this run
        # decided, and what did it decide for m1" — is not doubled.
        state = read_resume_state(ledger)
        assert len(state.outcomes) == 1
        assert state.outcomes[0].mission_id == "m1"
        assert state.outcomes[0].succeeded is True


class _SoakRuntime:
    async def run_subagent(self, **kwargs) -> AgentRunResult:
        return AgentRunResult(
            run=Run(run_id="r", spec=RunSpec(kind="k", input="i"), state=RunState.SUCCEEDED),
            output="ok",
            outcome=AgentOutcome.OK,
        )


class TestLongRunningSoak:
    def test_500_rounds_with_periodic_cold_restart_stay_uncorrupted(self, tmp_path: Path) -> None:
        orch = TeamOrchestrator(runtime=_SoakRuntime(), work_root=tmp_path)
        run = orch.create_run(
            CreateSpec(
                team_profile_id="soak",
                goal="500-round soak",
                agents=[{"agent_key": "a"}],
                coordination={"scheduling": {"mode": "eager", "max_parallel": 1}},
            )
        )
        board = orch.board_for(run.id)
        deps = orch.deps_for(run.id)
        run.state = TeamRunState.RUNNING

        n_rounds = 500
        restart_every = 47  # deliberately not a divisor of n_rounds
        for i in range(n_rounds):
            wid = f"w{i:04d}"
            board.post(WorkItem(id=wid, prompt=f"unit of work {i}", role_tags=["a"]))
            claimed = board.claim(wid, "a")
            assert claimed is not None
            board.mark_running(wid)
            board.complete(wid, result=f"result-{i}")
            run.active_round = i
            orch._store.save(run)
            snapshot_team_run(run, board=board, deps=deps)

            if (i + 1) % restart_every == 0:
                # Simulate a process crash + cold restart on a brand-new
                # orchestrator instance every `restart_every` rounds.
                orch = TeamOrchestrator(runtime=_SoakRuntime(), work_root=tmp_path)
                recovered = orch.cold_resume(run.id)
                assert recovered.active_round == i
                run = recovered
                board = orch.board_for(run.id)
                deps = orch.deps_for(run.id)

        items = board.all_items()
        assert len(items) == n_rounds
        assert all(item.state == WorkItemState.DONE for item in items)
        by_id = {item.id: item for item in items}
        for i in range(n_rounds):
            wid = f"w{i:04d}"
            # No corruption: every item's result matches exactly what was
            # written for it, regardless of how many cold restarts
            # happened after it completed. No replay: none of them were
            # re-claimed or re-run (an already-DONE item cannot transition
            # back to CLAIMED — see `WorkBoard._transit`).
            assert by_id[wid].result == f"result-{i}"

        # No unbounded growth: the snapshot is proportional to the work
        # actually done, not to the number of cold restarts along the way.
        snap_path = Path(run.work_root) / "team_run.json"
        assert snap_path.stat().st_size < 500_000

        # Cold-resuming one more time after the loop reproduces the exact
        # same terminal state (idempotent recovery).
        final_orch = TeamOrchestrator(runtime=_SoakRuntime(), work_root=tmp_path)
        final_run = final_orch.cold_resume(run.id)
        assert final_run.active_round == n_rounds - 1
        final_board = final_orch.board_for(run.id)
        assert len(final_board.all_items()) == n_rounds
        assert all(item.state == WorkItemState.DONE for item in final_board.all_items())


def test_ledger_tamper_is_detected_after_a_soak_style_run(tmp_path: Path) -> None:
    """Complements the session-tamper coverage in `test_session_sdk.py`
    with the institution-ledger side of "ledger/session tamper" (docs/28
    §7.2 exit criteria)."""
    ledger = EventLog(tmp_path / "l.jsonl", run_id="run-tamper")
    outcome = MissionOutcome(
        mission_id="m1",
        status="completed",
        team_run_id="tr_m1",
        rounds_used=1,
        wall_clock_seconds=0.1,
        contract_ok=True,
        contract_errors=[],
        paradigm="pipeline_chain",
    )
    record_mission_decision(ledger, outcome)
    assert verify_chain(ledger.path).ok

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    import json as _json

    tampered = _json.loads(lines[0])
    tampered["payload"]["subject"] = "m1-but-tampered"
    ledger.path.write_text(_json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")

    verdict = verify_chain(ledger.path)
    assert not verdict.ok
