"""Team-run domain models (employment loop + legacy Task projection)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TeamRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class AgentState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUSPENDED = "suspended"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkItemState(StrEnum):
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUSPENDED = "suspended"
    DONE = "done"
    FAILED = "failed"
    # Hard stop (merge conflict, policy). Not used for ordinary FS waits.
    BLOCKED = "blocked"
    # Soft wait: predecessors in the dep graph are not finished yet (role_pipeline).
    WAITING_DEPS = "waiting_deps"


class DepKind(StrEnum):
    FS = "FS"  # Finish-to-Start
    FF = "FF"  # Finish-to-Finish
    SS = "SS"  # Start-to-Start
    SF = "SF"  # Start-to-Finish


class AssessmentDecision(StrEnum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    WAITING_USER = "waiting_user"
    FAILED = "failed"


# Back-compat aliases used by orchestrator API surface
RoundAssessmentDecision = AssessmentDecision
TeamRunAgentStateValue = AgentState
TeamRunTaskState = TaskState


def is_terminal(state: TeamRunState) -> bool:
    return state in {TeamRunState.CANCELLED, TeamRunState.FAILED, TeamRunState.COMPLETED}


@dataclass
class AgentSlot:
    agent_key: str
    profile: dict
    work_dir: str
    state: AgentState = AgentState.IDLE
    current_task_id: str | None = None
    active_work_item_id: str | None = None
    replica_of: str | None = None
    task_memory_dir: str | None = None


@dataclass
class Task:
    """Legacy projection of a WorkItem for older assess/aggregation paths."""

    task_id: str
    agent_key: str
    prompt: str
    state: TaskState = TaskState.PENDING
    result: str = ""
    error: str | None = None
    round_index: int = 0
    facts: list[str] = field(default_factory=list)
    handoff_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkItem:
    id: str
    prompt: str
    role_tags: list[str] = field(default_factory=list)
    priority: int = 0
    state: WorkItemState = WorkItemState.READY
    assignee: str | None = None
    parent_id: str | None = None
    checkpoint_ref: str | None = None
    result: str = ""
    error: str | None = None
    urgent: bool = False
    wave: int = 0
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "role_tags": list(self.role_tags),
            "priority": self.priority,
            "state": self.state.value,
            "assignee": self.assignee,
            "parent_id": self.parent_id,
            "checkpoint_ref": self.checkpoint_ref,
            "result": self.result,
            "error": self.error,
            "urgent": self.urgent,
            "wave": self.wave,
            "weight": self.weight,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkItem:
        return cls(
            id=str(data["id"]),
            prompt=str(data.get("prompt") or ""),
            role_tags=[str(t) for t in (data.get("role_tags") or [])],
            priority=int(data.get("priority") or 0),
            state=WorkItemState(str(data.get("state") or WorkItemState.READY.value)),
            assignee=(str(data["assignee"]) if data.get("assignee") else None),
            parent_id=(str(data["parent_id"]) if data.get("parent_id") else None),
            checkpoint_ref=(str(data["checkpoint_ref"]) if data.get("checkpoint_ref") else None),
            result=str(data.get("result") or ""),
            error=(str(data["error"]) if data.get("error") else None),
            urgent=bool(data.get("urgent", False)),
            wave=int(data.get("wave") or 0),
            weight=float(data.get("weight") or 1.0),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class DepEdge:
    pred: str
    succ: str
    kind: DepKind = DepKind.FS

    def to_dict(self) -> dict[str, Any]:
        return {"pred": self.pred, "succ": self.succ, "kind": self.kind.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DepEdge:
        return cls(
            pred=str(data["pred"]),
            succ=str(data["succ"]),
            kind=DepKind(str(data.get("kind") or DepKind.FS.value)),
        )


@dataclass(frozen=True)
class RoundTask:
    agent_key: str
    prompt: str
    task_id: str | None = None
    handoff_to: str | None = None


@dataclass(frozen=True)
class RoundPlan:
    round_index: int
    tasks: list[RoundTask]
    rationale: str = ""
    needs_user_input: bool = False


@dataclass(frozen=True)
class RoundAssessment:
    decision: AssessmentDecision
    summary: str
    next_round_hint: str = ""
    preferred_agent: str | None = None
    artifact_plan: list[dict] = field(default_factory=list)
    reason_code: str = ""


@dataclass
class TeamRun:
    id: str
    goal: str
    team_profile_id: str
    state: TeamRunState
    active_round: int
    agents: list[AgentSlot]
    tasks: list[Task]
    work_root: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)

    @property
    def agent_states(self) -> list[AgentSlot]:
        return self.agents


def work_item_to_task(item: WorkItem, *, round_index: int = 0) -> Task:
    """Project a WorkItem onto the legacy Task shape for aggregation/assess."""
    state_map = {
        WorkItemState.READY: TaskState.PENDING,
        WorkItemState.CLAIMED: TaskState.RUNNING,
        WorkItemState.RUNNING: TaskState.RUNNING,
        WorkItemState.SUSPENDED: TaskState.PENDING,
        WorkItemState.DONE: TaskState.DONE,
        WorkItemState.FAILED: TaskState.FAILED,
        WorkItemState.BLOCKED: TaskState.PENDING,
        WorkItemState.WAITING_DEPS: TaskState.PENDING,
    }
    return Task(
        task_id=item.id,
        agent_key=item.assignee or (item.role_tags[0] if item.role_tags else ""),
        prompt=item.prompt,
        state=state_map.get(item.state, TaskState.PENDING),
        result=item.result,
        error=item.error,
        round_index=round_index or item.wave,
        metadata=dict(item.metadata),
    )


def agent_slot_to_dict(agent: AgentSlot) -> dict[str, Any]:
    return {
        "agent_key": agent.agent_key,
        "profile": dict(agent.profile),
        "work_dir": agent.work_dir,
        "state": agent.state.value,
        "current_task_id": agent.current_task_id,
        "active_work_item_id": agent.active_work_item_id,
        "replica_of": agent.replica_of,
        "task_memory_dir": agent.task_memory_dir,
    }
