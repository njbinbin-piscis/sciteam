"""Structured inbox and recoverable escalation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.models import TeamRunState, WorkItem, WorkItemState
from sciteam.orchestrator import CreateSpec, TeamOrchestrator
from sciteam.runtime import AgentOutcome, AgentRunResult, Run, RunSpec, RunState


class _HelpRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.worker_attempts = 0

    async def run_subagent(self, **kwargs) -> AgentRunResult:
        agent = str((kwargs.get("context") or {}).get("team_agent_key"))
        task = str(kwargs["task"])
        self.calls.append((agent, task))
        if agent == "worker" and self.worker_attempts < 2:
            self.worker_attempts += 1
            outcome = AgentOutcome.RECOVERABLE
            output = "contract mismatch"
        elif agent == "helper":
            outcome = AgentOutcome.OK
            output = "Use parser B and validate before emission."
        else:
            outcome = AgentOutcome.OK
            output = "repaired"
        run = Run(
            run_id=f"r{len(self.calls)}",
            spec=RunSpec(kind="test", input=task, agent_id=agent),
            state=RunState.SUCCEEDED if outcome == AgentOutcome.OK else RunState.FAILED,
            output=output,
            error=output if outcome != AgentOutcome.OK else None,
        )
        return AgentRunResult(
            run=run,
            output=output,
            outcome=outcome,
            reason_code="contract_mismatch" if outcome != AgentOutcome.OK else "",
            diagnosis=output if outcome != AgentOutcome.OK else "",
            retry_hint="change parser",
        )


def _orchestrator(tmp_path: Path, *, communication: str = "open"):
    runtime = _HelpRuntime()
    orchestrator = TeamOrchestrator(runtime=runtime, work_root=tmp_path)
    run = orchestrator.create_run(
        CreateSpec(
            team_profile_id="help",
            goal="repair",
            agents=[
                {"agent_key": "worker", "role": "worker"},
                {"agent_key": "helper", "role": "helper"},
            ],
            coordination={
                "communication": communication,
                "scheduling": {"mode": "eager", "max_parallel": 1},
                "stopping": {"max_iterations": 20},
            },
            metadata={"worker_retry_limit": 1},
        )
    )
    return runtime, orchestrator, run


@pytest.mark.asyncio
async def test_retry_exhaustion_routes_peer_help_and_resumes(tmp_path: Path) -> None:
    runtime, orchestrator, run = _orchestrator(tmp_path)
    board = orchestrator.board_for(run.id)
    board.post(WorkItem(id="work", prompt="produce contract", role_tags=["worker"]))

    await orchestrator.drive(run.id, max_ticks=8)

    original = board.get("work")
    help_items = [i for i in board.all_items() if i.metadata.get("help_request")]
    assert original is not None and original.state == WorkItemState.DONE
    assert help_items and help_items[0].state == WorkItemState.DONE
    assert any(agent == "worker" and "Previous attempt" in task for agent, task in runtime.calls)
    inbox = orchestrator.inbox_for(run.id, "helper")
    assert inbox and inbox[0]["source_agent"] == "worker"
    assert inbox[0]["reason_code"] == "contract_mismatch"


def test_closed_communication_fails_closed(tmp_path: Path) -> None:
    _, orchestrator, run = _orchestrator(tmp_path, communication="closed")
    with pytest.raises(PermissionError):
        orchestrator.send_message(
            run.id,
            source_agent="worker",
            target_agent="helper",
            text="secret",
        )
    assert orchestrator.inbox_for(run.id, "helper") == []


@pytest.mark.asyncio
async def test_closed_team_retry_exhaustion_pauses_without_ready_failed_loop(
    tmp_path: Path,
) -> None:
    runtime, orchestrator, run = _orchestrator(tmp_path, communication="closed")
    board = orchestrator.board_for(run.id)
    board.post(WorkItem(id="curator", prompt="emit JSON", role_tags=["worker"]))

    result = await orchestrator.drive(run.id, max_ticks=20)

    item = board.get("curator")
    assert result.state == TeamRunState.WAITING_USER
    assert item is not None and item.state == WorkItemState.SUSPENDED
    assert not board.by_state(WorkItemState.READY, WorkItemState.FAILED)
    assert runtime.worker_attempts == 2
    assert run.metadata["gate_reason"] == "worker_escalate"
    assert int(run.metadata["heartbeats_used"]) <= 2
