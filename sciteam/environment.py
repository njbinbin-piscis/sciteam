"""Shared environment view for employment heartbeats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sciteam.board import WorkBoard
from sciteam.coordination import CommunicationMode, CoordinationSpec
from sciteam.depgraph import DepGraph
from sciteam.models import AgentSlot, AgentState, TeamRun, WorkItem, WorkItemState


@dataclass(frozen=True)
class EnvSnapshot:
    agent_key: str
    goal: str
    charter: str
    ready: list[WorkItem]
    inbox: list[dict[str, Any]]
    wip: int
    budget_rounds_left: int
    communication: CommunicationMode
    critical_path: float
    self_state: AgentState


def observe(
    *,
    run: TeamRun,
    agent: AgentSlot,
    board: WorkBoard,
    deps: DepGraph,
    coordination: CoordinationSpec,
    inbox: list[dict[str, Any]] | None = None,
    budget_rounds_left: int = 0,
) -> EnvSnapshot:
    ready = deps.ready_set(board)
    matched: list[WorkItem] = []
    for item in ready:
        tags = item.role_tags or []
        if not tags:
            matched.append(item)
            continue
        role = str(agent.profile.get("role") or agent.agent_key)
        base = agent.replica_of or agent.agent_key
        if any(t in {agent.agent_key, base, role} for t in tags):
            matched.append(item)
    mail = list(inbox or [])
    if coordination.communication == CommunicationMode.CLOSED:
        mail = []
    wip = len(board.by_state(WorkItemState.CLAIMED, WorkItemState.RUNNING))
    return EnvSnapshot(
        agent_key=agent.agent_key,
        goal=run.goal,
        charter=coordination.charter.standing_goal or run.goal,
        ready=matched,
        inbox=mail,
        wip=wip,
        budget_rounds_left=budget_rounds_left,
        communication=coordination.communication,
        critical_path=deps.critical_path_length(board),
        self_state=agent.state,
    )
