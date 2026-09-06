"""Work-item failures feed diagnosis back before escalating."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.board import WorkBoard
from sciteam.coordination import CoordinationSpec
from sciteam.depgraph import DepGraph
from sciteam.heartbeat import AgentHeartbeat
from sciteam.models import (
    AgentSlot,
    TeamRun,
    TeamRunState,
    WorkItem,
    WorkItemState,
)
from sciteam.runtime import AgentOutcome, AgentRunResult, Run, RunSpec, RunState


class _SequenceRuntime:
    def __init__(self, outcomes: list[AgentOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.tasks: list[str] = []

    async def run_subagent(self, **kwargs) -> AgentRunResult:
        task = str(kwargs["task"])
        self.tasks.append(task)
        outcome = self.outcomes.pop(0)
        diagnosis = "invalid JSON from prior method" if outcome != AgentOutcome.OK else ""
        run = Run(
            run_id=f"r{len(self.tasks)}",
            spec=RunSpec(kind="test", input=task, agent_id=str(kwargs["agent_id"])),
            state=RunState.SUCCEEDED if outcome == AgentOutcome.OK else RunState.FAILED,
            output="done" if outcome == AgentOutcome.OK else diagnosis,
            error=diagnosis or None,
        )
        return AgentRunResult(
            run=run,
            output=run.output,
            outcome=outcome,
            reason_code="invalid_json_envelope" if diagnosis else "",
            diagnosis=diagnosis,
            retry_hint="emit one JSON object" if diagnosis else "",
        )


def _fixture(tmp_path: Path, runtime: _SequenceRuntime, *, retry_limit: int = 2):
    board = WorkBoard()
    board.post(WorkItem(id="w", prompt="solve", role_tags=["worker"]))
    run = TeamRun(
        id="team",
        goal="goal",
        team_profile_id="test",
        state=TeamRunState.RUNNING,
        active_round=1,
        agents=[],
        tasks=[],
        work_root=str(tmp_path / "team"),
        metadata={"worker_retry_limit": retry_limit},
    )
    agent = AgentSlot(
        agent_key="worker",
        profile={"agent_id": "worker"},
        work_dir=str(tmp_path / "worker"),
    )
    run.agents.append(agent)
    return AgentHeartbeat(runtime=runtime), run, agent, board, DepGraph()


@pytest.mark.asyncio
async def test_recoverable_result_requeues_with_diagnosis(tmp_path: Path) -> None:
    runtime = _SequenceRuntime([AgentOutcome.RECOVERABLE, AgentOutcome.OK])
    heartbeat, run, agent, board, deps = _fixture(tmp_path, runtime)

    first = await heartbeat.run_once(
        run=run,
        agent=agent,
        board=board,
        deps=deps,
        coordination=CoordinationSpec(),
    )
    item = board.get("w")
    assert first.completed is False
    assert item is not None and item.state == WorkItemState.READY
    assert item.metadata["retry_count"] == 1
    assert run.state == TeamRunState.RUNNING

    second = await heartbeat.run_once(
        run=run,
        agent=agent,
        board=board,
        deps=deps,
        coordination=CoordinationSpec(),
    )
    assert second.completed is True
    assert board.get("w").state == WorkItemState.DONE  # type: ignore[union-attr]
    assert "Previous attempt" in runtime.tasks[1]
    assert "invalid JSON" in runtime.tasks[1]


@pytest.mark.asyncio
async def test_retry_exhaustion_requests_help_instead_of_failing(tmp_path: Path) -> None:
    runtime = _SequenceRuntime([AgentOutcome.RECOVERABLE, AgentOutcome.RECOVERABLE])
    heartbeat, run, agent, board, deps = _fixture(tmp_path, runtime, retry_limit=1)
    for _ in range(2):
        result = await heartbeat.run_once(
            run=run,
            agent=agent,
            board=board,
            deps=deps,
            coordination=CoordinationSpec(),
        )

    item = board.get("w")
    assert result.decision.action.value == "escalate"
    assert item is not None and item.state == WorkItemState.SUSPENDED
    assert run.state == TeamRunState.RUNNING
    assert "worker_escalation" in run.metadata
