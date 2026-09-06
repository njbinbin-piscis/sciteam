"""Employment heartbeat: observe → decide → claim → run → complete."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sciteam.board import WorkBoard
from sciteam.claim_policy import ClaimActionKind, ClaimDecision, ClaimPolicy, ScriptedClaimPolicy
from sciteam.coordination import CoordinationSpec, UrgentPolicy
from sciteam.depgraph import DepGraph
from sciteam.environment import observe
from sciteam.merge import MergeClerk, MergeDelta, write_delta
from sciteam.models import (
    AgentSlot,
    AgentState,
    TeamRun,
    WorkItem,
    WorkItemState,
    work_item_to_task,
)
from sciteam.persist import checkpoint_work_item, snapshot_team_run
from sciteam.runtime import AgentOutcome, RuntimePort


def _pipeline_predecessor_digest(
    board: WorkBoard,
    item: WorkItem,
    coordination: CoordinationSpec,
) -> str:
    """Same-wave done results from earlier role_pipeline seats (institutional handoff)."""
    pipe = list(coordination.work_graph.role_pipeline or ())
    if not pipe:
        return ""
    tags = [str(t) for t in (item.role_tags or []) if t]
    role = tags[0] if tags else ""
    if role not in pipe:
        return ""
    idx = pipe.index(role)
    if idx <= 0:
        return ""
    wanted = set(pipe[:idx])
    chunks: list[str] = []
    for other in board.all_items():
        if other.wave != item.wave:
            continue
        otags = [str(t) for t in (other.role_tags or []) if t]
        orole = otags[0] if otags else (other.assignee or "")
        if orole not in wanted:
            continue
        if other.state not in {WorkItemState.DONE, WorkItemState.FAILED}:
            continue
        body = (other.result or other.error or "")[:1500]
        chunks.append(f"### prior:{orole} ({other.state.value})\n{body}")
    if not chunks:
        return ""
    return "# Predecessor outputs (same wave; consume before acting)\n" + "\n\n".join(chunks)


def _flush_board(run: TeamRun, board: WorkBoard, deps: DepGraph | None = None) -> None:
    """Persist board mid-tick so observatory can see ready/running before wave ends."""
    run.tasks = project_tasks(board, round_index=run.active_round)
    try:
        snapshot_team_run(run, board=board, deps=deps)
    except OSError:
        pass


@dataclass
class HeartbeatResult:
    agent_key: str
    decision: ClaimDecision
    work_item_id: str | None = None
    completed: bool = False
    spawned: str | None = None
    message: str = ""


class AgentHeartbeat:
    def __init__(
        self,
        *,
        runtime: RuntimePort,
        claim_policy: ClaimPolicy | None = None,
        merge_clerk: MergeClerk | None = None,
    ) -> None:
        self._runtime = runtime
        self._policy = claim_policy or ScriptedClaimPolicy()
        self._merge = merge_clerk or MergeClerk()

    @staticmethod
    def _retry_limit(run: TeamRun) -> int:
        raw = run.metadata.get("worker_retry_limit", 2)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _retry_context(item: WorkItem) -> str:
        retry_count = int(item.metadata.get("retry_count") or 0)
        peer_help = str(item.metadata.get("peer_help") or "")
        if retry_count <= 0 and not peer_help:
            return ""
        return (
            "# Previous attempt did not satisfy the work contract\n"
            f"attempts: {retry_count}\n"
            f"reason_code: {item.metadata.get('last_reason_code') or 'recoverable_error'}\n"
            f"diagnosis: {item.metadata.get('diagnosis') or item.error or ''}\n"
            f"repair guidance: {item.metadata.get('retry_hint') or 'change method and retry'}\n"
            f"peer/supervisor response: {peer_help}\n"
            "Do not repeat the failed method unchanged. Preserve useful partial work, "
            "then produce a valid final envelope."
        )

    @staticmethod
    def _write_diagnosis_dossier(agent: AgentSlot, item: WorkItem) -> str:
        """Render the repair history to disk so `diagnosis_ref` resolves.

        Rewritten in full from `metadata["attempts"]` on every failure, so the
        file is a deterministic projection of board state rather than an
        append log that could drift from it.
        """
        attempts = list(item.metadata.get("attempts") or [])
        if not attempts:
            return ""
        target = agent.task_memory_dir or str(Path(agent.work_dir) / "memory" / item.id)
        lines = [f"# Repair dossier — work item {item.id}", ""]
        for record in attempts:
            lines.append(f"## Attempt {record.get('attempt')}")
            lines.append(f"- reason_code: {record.get('reason_code') or ''}")
            lines.append(f"- retry_hint: {record.get('retry_hint') or ''}")
            lines.append("")
            lines.append(str(record.get("diagnosis") or ""))
            lines.append("")
        try:
            path = Path(target)
            path.mkdir(parents=True, exist_ok=True)
            dossier = path / "diagnosis.md"
            dossier.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            return ""
        return str(dossier)

    def _recover_or_escalate(
        self,
        *,
        run: TeamRun,
        agent: AgentSlot,
        board: WorkBoard,
        deps: DepGraph | None,
        item: WorkItem,
        reason_code: str,
        diagnosis: str,
        retry_hint: str,
    ) -> HeartbeatResult:
        next_count = int(item.metadata.get("retry_count") or 0) + 1
        limit = self._retry_limit(run)
        agent.state = AgentState.IDLE
        agent.active_work_item_id = None
        agent.current_task_id = None
        if next_count <= limit:
            board.requeue(
                item.id,
                reason_code=reason_code,
                diagnosis=diagnosis,
                retry_hint=retry_hint,
            )
            item.metadata["diagnosis_ref"] = self._write_diagnosis_dossier(agent, item)
            _flush_board(run, board, deps)
            return HeartbeatResult(
                agent_key=agent.agent_key,
                decision=ClaimDecision(
                    ClaimActionKind.CLAIM,
                    work_item_id=item.id,
                    reason="recoverable work-item error",
                ),
                work_item_id=item.id,
                completed=False,
                message=f"requeued ({next_count}/{limit}): {diagnosis}",
            )

        board.suspend(item.id, checkpoint_ref=agent.task_memory_dir)
        item.metadata["retry_count"] = next_count
        item.metadata["last_reason_code"] = reason_code
        item.metadata["diagnosis"] = diagnosis[:4000]
        item.metadata["retry_hint"] = retry_hint[:2000]
        # The escalating attempt itself was never appended by `board.requeue`
        # (we never call it — we go straight to suspend), so without this the
        # final, most-informative failure is missing from the dossier.
        attempts = list(item.metadata.get("attempts") or [])
        attempts.append(
            {
                "attempt": next_count,
                "reason_code": str(reason_code or "worker_retry_exhausted"),
                "diagnosis": diagnosis[:4000],
                "retry_hint": retry_hint[:2000],
            }
        )
        item.metadata["attempts"] = attempts[-10:]
        item.metadata["diagnosis_ref"] = self._write_diagnosis_dossier(agent, item)
        escalation = {
            "source_agent": agent.agent_key,
            "work_item_id": item.id,
            "reason_code": reason_code or "worker_retry_exhausted",
            "diagnosis": diagnosis[:4000],
            "attempts": next_count,
            "target": "supervisor",
        }
        run.metadata["worker_escalation"] = escalation
        _flush_board(run, board, deps)
        return HeartbeatResult(
            agent_key=agent.agent_key,
            decision=ClaimDecision(
                ClaimActionKind.ESCALATE,
                work_item_id=item.id,
                reason=reason_code or "worker_retry_exhausted",
                target="supervisor",
                question=(
                    f"Help repair work item {item.id}: {diagnosis}. Prior guidance: {retry_hint}"
                ),
            ),
            work_item_id=item.id,
            completed=False,
            message=f"escalated after {next_count} attempts: {diagnosis}",
        )

    async def run_once(
        self,
        *,
        run: TeamRun,
        agent: AgentSlot,
        board: WorkBoard,
        deps: DepGraph,
        coordination: CoordinationSpec,
        inbox: list[dict[str, Any]] | None = None,
        budget_rounds_left: int = 0,
        spawn_replica: Any | None = None,
    ) -> HeartbeatResult:
        if agent.state == AgentState.RUNNING:
            # Allow preempt/fork decision while busy if urgent ready work exists.
            snapshot = observe(
                run=run,
                agent=agent,
                board=board,
                deps=deps,
                coordination=coordination,
                inbox=inbox,
                budget_rounds_left=budget_rounds_left,
            )
            urgent_ready = [i for i in snapshot.ready if i.urgent]
            if not urgent_ready:
                return HeartbeatResult(
                    agent_key=agent.agent_key,
                    decision=ClaimDecision(ClaimActionKind.REST, reason="busy"),
                    message="already running",
                )
            item = urgent_ready[0]
            urgent_policy = coordination.scheduling.resolved_urgent_policy()
            if urgent_policy == UrgentPolicy.FORK and spawn_replica is not None:
                replica = spawn_replica(agent, item)
                if replica is not None:
                    return HeartbeatResult(
                        agent_key=agent.agent_key,
                        decision=ClaimDecision(
                            ClaimActionKind.CLAIM, work_item_id=item.id, reason="fork"
                        ),
                        work_item_id=item.id,
                        spawned=replica.agent_key,
                        message="forked replica for urgent work",
                    )
            # preempt
            old_id = agent.active_work_item_id
            if old_id:
                ref = checkpoint_work_item(run, old_id, agent=agent)
                board.suspend(old_id, checkpoint_ref=ref)
            agent.state = AgentState.IDLE
            agent.active_work_item_id = None
            claimed = board.claim(item.id, agent.agent_key)
            if claimed is None:
                return HeartbeatResult(
                    agent_key=agent.agent_key,
                    decision=ClaimDecision(ClaimActionKind.REST, reason="urgent claim lost"),
                    message="urgent claim lost",
                )
            return await self._execute_claimed(
                run=run,
                agent=agent,
                board=board,
                deps=deps,
                item=claimed,
                coordination=coordination,
            )

        if agent.state == AgentState.SUSPENDED:
            return HeartbeatResult(
                agent_key=agent.agent_key,
                decision=ClaimDecision(ClaimActionKind.REST, reason="suspended"),
                message="suspended awaiting resume",
            )

        snapshot = observe(
            run=run,
            agent=agent,
            board=board,
            deps=deps,
            coordination=coordination,
            inbox=inbox,
            budget_rounds_left=budget_rounds_left,
        )
        decision = await self._policy.decide(snapshot)
        if decision.action != ClaimActionKind.CLAIM or not decision.work_item_id:
            return HeartbeatResult(
                agent_key=agent.agent_key,
                decision=decision,
                message=decision.reason or decision.action.value,
            )

        item = board.get(decision.work_item_id)
        if item is None:
            return HeartbeatResult(
                agent_key=agent.agent_key,
                decision=decision,
                message="missing work item",
            )

        claimed = board.claim(item.id, agent.agent_key)
        if claimed is None:
            return HeartbeatResult(
                agent_key=agent.agent_key,
                decision=decision,
                message="claim lost race",
            )

        return await self._execute_claimed(
            run=run,
            agent=agent,
            board=board,
            deps=deps,
            item=claimed,
            coordination=coordination,
        )

    async def _execute_claimed(
        self,
        *,
        run: TeamRun,
        agent: AgentSlot,
        board: WorkBoard,
        deps: DepGraph | None,
        item: WorkItem,
        coordination: CoordinationSpec,
    ) -> HeartbeatResult:
        board.mark_running(item.id)
        agent.state = AgentState.RUNNING
        agent.active_work_item_id = item.id
        agent.current_task_id = item.id
        mem = Path(agent.work_dir) / "memory" / item.id
        mem.mkdir(parents=True, exist_ok=True)
        agent.task_memory_dir = str(mem)
        # Flush after claim/running so UI can show N/(N+1) while LLM works.
        _flush_board(run, board, deps)

        try:
            pred = _pipeline_predecessor_digest(board, item, coordination)
            task_text = item.prompt
            if pred:
                task_text = f"{item.prompt}\n\n{pred}"
            retry_context = self._retry_context(item)
            if retry_context:
                task_text = f"{task_text}\n\n{retry_context}"
            result = await self._runtime.run_subagent(
                agent_id=str(agent.profile.get("agent_id") or agent.agent_key),
                task=task_text,
                work_dir=agent.work_dir,
                parent_run_id=run.id,
                depth=0,
                context={
                    "team_run_id": run.id,
                    "team_agent_key": agent.agent_key,
                    "team_profile_id": run.team_profile_id,
                    "team_goal": run.goal,
                    "team_metadata": dict(run.metadata),
                    "work_item_id": item.id,
                    "task_memory_dir": agent.task_memory_dir,
                    "round_index": item.wave,
                    "communication": coordination.communication.value,
                    "predecessor_digest": pred,
                    "retry_count": int(item.metadata.get("retry_count") or 0),
                    "retry_diagnosis": str(item.metadata.get("diagnosis") or ""),
                },
            )
            if result.outcome == AgentOutcome.RECOVERABLE:
                return self._recover_or_escalate(
                    run=run,
                    agent=agent,
                    board=board,
                    deps=deps,
                    item=item,
                    reason_code=result.reason_code or "recoverable_error",
                    diagnosis=result.diagnosis or result.output or "recoverable error",
                    retry_hint=result.retry_hint or "change method and retry",
                )
            if result.outcome == AgentOutcome.FATAL:
                board.fail(
                    item.id,
                    error=result.diagnosis
                    or result.output
                    or result.reason_code
                    or "fatal agent error",
                )
                agent.state = AgentState.FAILED
                agent.active_work_item_id = None
                agent.current_task_id = None
                _flush_board(run, board, deps)
                return HeartbeatResult(
                    agent_key=agent.agent_key,
                    decision=ClaimDecision(
                        ClaimActionKind.CLAIM,
                        work_item_id=item.id,
                        reason=result.reason_code or "fatal_agent_error",
                    ),
                    work_item_id=item.id,
                    completed=False,
                    message=result.diagnosis or result.output or "fatal agent error",
                )
            board.complete(item.id, result=result.output)
            # Replica delta for merge
            if agent.replica_of:
                touched = [
                    p.name
                    for p in Path(agent.work_dir).iterdir()
                    if p.is_file() and p.suffix in {".py", ".json", ".md", ".txt"}
                ]
                delta = MergeDelta(
                    work_item_id=item.id,
                    replica_key=agent.agent_key,
                    touched_paths=touched,
                    source_dir=agent.work_dir,
                )
                write_delta(Path(run.work_root) / "merges" / f"{item.id}_delta.json", delta)
            agent.state = AgentState.IDLE
            agent.active_work_item_id = None
            agent.current_task_id = None
            # Resume suspended parent work if this was a preempted agent finishing urgent
            suspended = [
                i
                for i in board.all_items()
                if i.state == WorkItemState.SUSPENDED and i.assignee == agent.agent_key
            ]
            for s in suspended:
                board.resume(s.id)
            if item.parent_id and item.metadata.get("help_request"):
                parent = board.get(item.parent_id)
                if parent is not None and parent.state == WorkItemState.SUSPENDED:
                    parent.metadata["peer_help"] = result.output[:4000]
                    parent.metadata["retry_hint"] = (
                        "Use this peer/supervisor response, then change method:\n"
                        + result.output[:4000]
                    )
                    parent.metadata["retry_count"] = 0
                    board.resume(parent.id)
            _flush_board(run, board, deps)
            return HeartbeatResult(
                agent_key=agent.agent_key,
                decision=ClaimDecision(ClaimActionKind.CLAIM, work_item_id=item.id),
                work_item_id=item.id,
                completed=True,
                message="completed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._recover_or_escalate(
                run=run,
                agent=agent,
                board=board,
                deps=deps,
                item=item,
                reason_code="runtime_exception",
                diagnosis=f"{type(exc).__name__}: {exc}",
                retry_hint="inspect the exception, change method, and re-emit the contract",
            )


def project_tasks(board: WorkBoard, *, round_index: int = 0) -> list:
    return [work_item_to_task(i, round_index=round_index or i.wave) for i in board.all_items()]
