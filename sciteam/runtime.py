"""Worker runtime port and run records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentOutcome(StrEnum):
    """Disposition of one claimed work item.

    Runtime failures are not automatically scientific failures.  RECOVERABLE
    asks the employment loop to feed the diagnosis back and retry; FATAL is
    reserved for faults that cannot be repaired inside the current run.
    """

    OK = "ok"
    RECOVERABLE = "recoverable"
    FATAL = "fatal"


@dataclass(frozen=True)
class RunSpec:
    kind: str
    input: str
    agent_id: str | None = None
    parent_run_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Run:
    run_id: str
    spec: RunSpec
    state: RunState
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    output: str = ""
    error: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    run: Run
    output: str
    outcome: AgentOutcome = AgentOutcome.OK
    reason_code: str = ""
    diagnosis: str = ""
    retry_hint: str = ""


@runtime_checkable
class RuntimePort(Protocol):
    async def run_subagent(
        self,
        *,
        agent_id: str,
        task: str,
        work_dir: str,
        parent_run_id: str | None = None,
        depth: int = 0,
        context: dict[str, Any] | None = None,
    ) -> AgentRunResult: ...
