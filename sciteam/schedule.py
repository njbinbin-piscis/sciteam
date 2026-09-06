"""Schedule policies — when to dispatch and when to assess."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sciteam.coordination import BusyPolicy, CoordinationSpec, SchedulingMode
from sciteam.models import AgentState, Task, TaskState, TeamRun


@runtime_checkable
class SchedulePolicy(Protocol):
    def select_dispatch(self, run: TeamRun, spec: CoordinationSpec) -> list[Task]: ...

    def should_assess(self, run: TeamRun, spec: CoordinationSpec) -> bool: ...

    def has_work(self, run: TeamRun) -> bool: ...


def _round_tasks(run: TeamRun) -> list[Task]:
    return [t for t in run.tasks if t.round_index == run.active_round]


def _agent(run: TeamRun, task: Task):
    for agent in run.agents:
        if agent.agent_key == task.agent_key:
            return agent
    return None


def _prefer_handoff(run: TeamRun, candidates: list[Task]) -> list[Task]:
    preferred = str((run.metadata or {}).get("preferred_agent") or "").strip()
    if not preferred or len(candidates) <= 1:
        return candidates
    head = [t for t in candidates if t.agent_key == preferred]
    tail = [t for t in candidates if t.agent_key != preferred]
    return head + tail


def _round_terminal(current: list[Task]) -> bool:
    """True when every task in the round is done or failed (no pending/running)."""
    return bool(current) and all(
        t.state in {TaskState.DONE, TaskState.FAILED} for t in current
    )


class BarrierSchedulePolicy:
    def select_dispatch(self, run: TeamRun, spec: CoordinationSpec) -> list[Task]:
        del spec
        candidates = [
            t
            for t in run.tasks
            if t.state == TaskState.PENDING and t.round_index == run.active_round
        ]
        return _prefer_handoff(run, candidates)

    def should_assess(self, run: TeamRun, spec: CoordinationSpec) -> bool:
        del spec
        return _round_terminal(_round_tasks(run))

    def has_work(self, run: TeamRun) -> bool:
        return any(t.state == TaskState.PENDING for t in run.tasks)


class EagerSchedulePolicy:
    def select_dispatch(self, run: TeamRun, spec: CoordinationSpec) -> list[Task]:
        ready: list[Task] = []
        for task in run.tasks:
            if task.state != TaskState.PENDING or task.round_index != run.active_round:
                continue
            agent = _agent(run, task)
            if agent is None or agent.state in {AgentState.IDLE, AgentState.DONE}:
                ready.append(task)
            elif spec.scheduling.busy_policy == BusyPolicy.CLONE:
                continue
        return _prefer_handoff(run, ready)

    def should_assess(self, run: TeamRun, spec: CoordinationSpec) -> bool:
        del spec
        if any(t.state == TaskState.RUNNING for t in run.tasks):
            return False
        current = _round_tasks(run)
        if not current or any(t.state == TaskState.PENDING for t in current):
            return False
        return _round_terminal(current)

    def has_work(self, run: TeamRun) -> bool:
        return any(t.state == TaskState.PENDING for t in run.tasks)


def resolve_schedule_policy(spec: CoordinationSpec) -> SchedulePolicy:
    if spec.scheduling.mode == SchedulingMode.EAGER:
        return EagerSchedulePolicy()
    return BarrierSchedulePolicy()
