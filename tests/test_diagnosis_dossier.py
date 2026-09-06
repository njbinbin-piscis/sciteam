"""P0-1: `diagnosis_ref` must resolve to a real dossier on BOTH the requeue
(retry) path and the suspend (escalation) path — not just requeue.

Regression target: before this fix, `_recover_or_escalate`'s suspend branch
suspended the item, recorded `diagnosis`/`retry_hint` on metadata, but never
appended the escalating attempt to `metadata["attempts"]` nor wrote
`diagnosis_ref`. `assessment_facts.py` exposed an empty `diagnosis_ref` for
every escalated item — an observability field that looked wired but wasn't.
"""

from __future__ import annotations

from pathlib import Path

from sciteam.board import WorkBoard
from sciteam.heartbeat import AgentHeartbeat
from sciteam.models import (
    AgentSlot,
    TeamRun,
    TeamRunState,
    WorkItem,
    WorkItemState,
)
from sciteam.runtime import RuntimePort


class _NoopRuntime(RuntimePort):
    async def run_subagent(self, **kwargs):  # pragma: no cover - unused in these tests
        raise NotImplementedError


def _run_agent_item(tmp_path: Path, *, worker_retry_limit: int = 1):
    run = TeamRun(
        id="run1",
        goal="g",
        team_profile_id="p",
        state=TeamRunState.RUNNING,
        active_round=0,
        agents=[],
        tasks=[],
        work_root=str(tmp_path),
        metadata={"worker_retry_limit": worker_retry_limit},
    )
    work_dir = tmp_path / "agent_work"
    agent = AgentSlot(agent_key="worker", profile={}, work_dir=str(work_dir))
    task_mem = work_dir / "memory" / "item1"
    task_mem.mkdir(parents=True, exist_ok=True)
    agent.task_memory_dir = str(task_mem)
    board = WorkBoard()
    item = WorkItem(id="item1", prompt="do the thing", role_tags=["worker"])
    board.post(item)
    board.claim(item.id, agent.agent_key)
    return run, agent, board, item


def test_requeue_path_sets_diagnosis_ref(tmp_path: Path) -> None:
    run, agent, board, item = _run_agent_item(tmp_path, worker_retry_limit=2)
    heartbeat = AgentHeartbeat(runtime=_NoopRuntime())

    result = heartbeat._recover_or_escalate(
        run=run,
        agent=agent,
        board=board,
        deps=None,
        item=item,
        reason_code="contract_mismatch",
        diagnosis="candidate missing Policy class",
        retry_hint="define a module-level Policy class",
    )

    assert result.decision.action.value == "claim"
    ref = item.metadata.get("diagnosis_ref")
    assert ref, "diagnosis_ref must be set after requeue"
    dossier = Path(ref)
    assert dossier.is_file()
    text = dossier.read_text(encoding="utf-8")
    assert "contract_mismatch" in text
    assert "candidate missing Policy class" in text
    assert item.state == WorkItemState.READY


def test_suspend_escalation_path_sets_diagnosis_ref(tmp_path: Path) -> None:
    """The suspend/escalate branch never calls `board.requeue`, so the final,
    most informative failure used to be silently dropped from the dossier."""
    run, agent, board, item = _run_agent_item(tmp_path, worker_retry_limit=0)
    heartbeat = AgentHeartbeat(runtime=_NoopRuntime())

    result = heartbeat._recover_or_escalate(
        run=run,
        agent=agent,
        board=board,
        deps=None,
        item=item,
        reason_code="worker_retry_exhausted",
        diagnosis="candidate still fails frozen eval after 2 attempts",
        retry_hint="re-read the eval interface doc",
    )

    assert result.decision.action.value == "escalate"
    assert item.state == WorkItemState.SUSPENDED
    ref = item.metadata.get("diagnosis_ref")
    assert ref, "diagnosis_ref must be set on the suspend/escalation path too"
    dossier = Path(ref)
    assert dossier.is_file()
    text = dossier.read_text(encoding="utf-8")
    assert "worker_retry_exhausted" in text
    assert "candidate still fails frozen eval after 2 attempts" in text
    attempts = item.metadata.get("attempts") or []
    assert attempts and attempts[-1]["reason_code"] == "worker_retry_exhausted"


def test_diagnosis_ref_flows_into_assessment_facts(tmp_path: Path) -> None:
    from sciteam.assessment_facts import build_assessment_facts
    from sciteam.models import Task, TaskState

    run, agent, board, item = _run_agent_item(tmp_path, worker_retry_limit=0)
    heartbeat = AgentHeartbeat(runtime=_NoopRuntime())
    heartbeat._recover_or_escalate(
        run=run,
        agent=agent,
        board=board,
        deps=None,
        item=item,
        reason_code="worker_retry_exhausted",
        diagnosis="frozen eval still failing",
        retry_hint="check Policy interface",
    )
    run.tasks = [
        Task(
            task_id=item.id,
            agent_key=agent.agent_key,
            prompt=item.prompt,
            state=TaskState.FAILED,
            metadata=dict(item.metadata),
        )
    ]

    facts = build_assessment_facts(run)
    rows = [t for t in facts.get("wave_tasks", []) if t.get("agent_key") == agent.agent_key]
    assert rows, "expected the escalated task to be present in assessment facts"
    assert rows[0]["diagnosis_ref"], "assessment_facts must expose a non-empty diagnosis_ref"
