"""Team orchestrator: employment heartbeat loop (v0.3)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sciteam.aggregation import apply_aggregation
from sciteam.board import WorkBoard
from sciteam.claim_policy import ClaimPolicy, ScriptedClaimPolicy
from sciteam.coordination import CoordinationSpec, SchedulingMode
from sciteam.coordinator import ScriptedCoordinator, TeamCoordinator
from sciteam.depgraph import DepGraph, compile_barrier_waves
from sciteam.guard import TeamGuard
from sciteam.heartbeat import AgentHeartbeat, project_tasks
from sciteam.merge import MergeClerk, read_delta
from sciteam.models import (
    AgentSlot,
    AgentState,
    AssessmentDecision,
    DepEdge,
    DepKind,
    TeamRun,
    TeamRunState,
    WorkItem,
    WorkItemState,
    is_terminal,
)
from sciteam.persist import snapshot_team_run
from sciteam.runtime import RuntimePort
from sciteam.store import MemoryStore


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class CreateSpec:
    team_profile_id: str
    goal: str
    agents: list[dict[str, Any]]
    coordination: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class DriveResult:
    team_run_id: str
    state: TeamRunState
    dispatched: int
    message: str


class TeamOrchestrator:
    def __init__(
        self,
        *,
        runtime: RuntimePort,
        work_root: str | Path,
        store: MemoryStore | None = None,
        coordinator: TeamCoordinator | None = None,
        guard: TeamGuard | None = None,
        claim_policy: ClaimPolicy | None = None,
    ) -> None:
        self._runtime = runtime
        self._work_root = Path(work_root)
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._store = store or MemoryStore()
        self._coordinator = coordinator or ScriptedCoordinator()
        self._guard = guard or TeamGuard()
        self._boards: dict[str, WorkBoard] = {}
        self._deps: dict[str, DepGraph] = {}
        self._inboxes: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._heartbeat = AgentHeartbeat(
            runtime=runtime,
            claim_policy=claim_policy or ScriptedClaimPolicy(),
            merge_clerk=MergeClerk(),
        )

    def board_for(self, team_run_id: str) -> WorkBoard:
        if team_run_id not in self._boards:
            self._boards[team_run_id] = WorkBoard()
        return self._boards[team_run_id]

    def deps_for(self, team_run_id: str) -> DepGraph:
        if team_run_id not in self._deps:
            self._deps[team_run_id] = DepGraph()
        return self._deps[team_run_id]

    def send_message(
        self,
        team_run_id: str,
        *,
        source_agent: str,
        target_agent: str,
        text: str,
        kind: str = "help",
        work_item_id: str | None = None,
        reason_code: str = "",
        evidence_refs: list[str] | tuple[str, ...] = (),
        ttl: int = 8,
    ) -> dict[str, Any]:
        """Deliver an auditable team message when the paradigm permits it."""
        run = self._store.require(team_run_id)
        coordination = CoordinationSpec.from_run_metadata(run.metadata)
        if coordination.communication.value != "open":
            raise PermissionError("team communication is closed")
        if not any(a.agent_key == target_agent for a in run.agents):
            raise KeyError(f"unknown target agent: {target_agent}")
        message = {
            "id": _new_id("msg"),
            "source_agent": str(source_agent),
            "target_agent": str(target_agent),
            "kind": str(kind or "help"),
            "text": str(text or "")[:8000],
            "work_item_id": str(work_item_id or ""),
            "reason_code": str(reason_code or ""),
            "evidence_refs": [str(x) for x in evidence_refs if str(x)][:20],
            "ttl": max(1, int(ttl)),
            "created_at": datetime.now(UTC).isoformat(),
        }
        boxes = self._inboxes.setdefault(team_run_id, {})
        boxes.setdefault(target_agent, []).append(message)
        log = list(run.metadata.get("inbox_log") or [])
        log.append(message)
        run.metadata["inbox_log"] = log[-100:]
        self._store.save(run)
        return message

    def inbox_for(self, team_run_id: str, agent_key: str) -> list[dict[str, Any]]:
        boxes = self._inboxes.setdefault(team_run_id, {})
        return [
            dict(message)
            for message in boxes.get(agent_key, [])
            if int(message.get("ttl") or 0) > 0
        ]

    def drain_inbox(self, team_run_id: str, agent_key: str) -> list[dict[str, Any]]:
        boxes = self._inboxes.setdefault(team_run_id, {})
        messages = self.inbox_for(team_run_id, agent_key)
        boxes[agent_key] = []
        return messages

    @staticmethod
    def _is_assess_agent(agent: AgentSlot, assess_roles: set[str]) -> bool:
        role = str(agent.profile.get("role") or agent.agent_key)
        auth = set(str(x) for x in (agent.profile.get("authority") or []))
        return agent.agent_key in assess_roles or role in assess_roles or "may_assess_round" in auth

    def _resolve_help_target(
        self,
        run: TeamRun,
        *,
        source_agent: str,
        requested: str | None,
    ) -> str | None:
        token = str(requested or "supervisor")
        if token == "human":
            return None
        by_key = {a.agent_key: a for a in run.agents}
        if token in by_key and token != source_agent:
            return token
        assess_roles = {str(x) for x in (run.metadata.get("assess_roles") or []) if str(x)}
        supervisor_roles = {str(x) for x in (run.metadata.get("supervisor_roles") or []) if str(x)}
        if token == "supervisor" and supervisor_roles:
            for agent in run.agents:
                role = str(agent.profile.get("role") or agent.agent_key)
                if agent.agent_key != source_agent and (
                    agent.agent_key in supervisor_roles or role in supervisor_roles
                ):
                    return agent.agent_key
        for agent in run.agents:
            if agent.agent_key == source_agent:
                continue
            if not self._is_assess_agent(agent, assess_roles):
                return agent.agent_key
        return None

    def _route_escalation(
        self,
        run: TeamRun,
        board: WorkBoard,
        result: Any,
        coordination: CoordinationSpec,
    ) -> None:
        decision = result.decision
        target = self._resolve_help_target(
            run,
            source_agent=result.agent_key,
            requested=getattr(decision, "target", None),
        )
        if target is None or coordination.communication.value != "open":
            run.state = TeamRunState.WAITING_USER
            run.metadata["gate_reason"] = "worker_escalate"
            run.metadata["human_gate_pending"] = True
            run.metadata["worker_escalation"] = {
                "source_agent": result.agent_key,
                "work_item_id": result.work_item_id or "",
                "reason_code": decision.reason or "worker_escalate",
                "question": getattr(decision, "question", "") or result.message,
                "target": "human",
            }
            return

        question = getattr(decision, "question", "") or result.message
        self.send_message(
            run.id,
            source_agent=result.agent_key,
            target_agent=target,
            text=question,
            kind="help_request",
            work_item_id=result.work_item_id,
            reason_code=decision.reason,
            evidence_refs=getattr(decision, "evidence_refs", ()),
        )
        help_item = WorkItem(
            id=_new_id("help"),
            prompt=(
                "# Peer/supervisor help request\n"
                f"From: {result.agent_key}\n"
                f"Reason: {decision.reason}\n"
                f"Question: {question}\n"
                "Return a concrete diagnosis and a different repair method. "
                "Do not take over the mission exit artifact unless your seat has emit duty."
            ),
            role_tags=[target],
            priority=10_000,
            urgent=True,
            wave=run.active_round,
            parent_id=result.work_item_id,
            metadata={
                "help_request": True,
                "source_agent": result.agent_key,
                "reason_code": decision.reason,
            },
        )
        board.post(help_item)

    def create_run(self, spec: CreateSpec) -> TeamRun:
        run_id = _new_id("team")
        root = self._work_root / run_id
        root.mkdir(parents=True, exist_ok=True)
        agents: list[AgentSlot] = []
        for item in spec.agents:
            key = str(item.get("agent_key") or item.get("key") or "")
            if not key:
                raise ValueError("agent missing agent_key")
            work_dir = root / key
            work_dir.mkdir(parents=True, exist_ok=True)
            agents.append(
                AgentSlot(
                    agent_key=key,
                    profile=dict(item),
                    work_dir=str(work_dir),
                    task_memory_dir=str(work_dir / "memory"),
                )
            )
        coordination = CoordinationSpec.from_dict(spec.coordination)
        metadata = dict(spec.metadata or {})
        metadata["coordination"] = coordination.to_dict()
        metadata["heartbeats_used"] = 0
        run = TeamRun(
            id=run_id,
            goal=spec.goal,
            team_profile_id=spec.team_profile_id,
            state=TeamRunState.PENDING,
            active_round=0,
            agents=agents,
            tasks=[],
            work_root=str(root),
            metadata=metadata,
        )
        board = self.board_for(run_id)
        deps = self.deps_for(run_id)
        if coordination.work_graph.edges:
            deps.extend(list(coordination.work_graph.edges))
        self._store.save(run)
        return run

    def _post_plan_to_board(self, run: TeamRun, plan_tasks: list, *, wave: int) -> list[str]:
        board = self.board_for(run.id)
        ids: list[str] = []
        for item in plan_tasks:
            wid = item.task_id or _new_id("wi")
            role = item.agent_key
            board.post(
                WorkItem(
                    id=wid,
                    prompt=item.prompt,
                    role_tags=[role] if role else [],
                    priority=10 if getattr(item, "urgent", False) else 0,
                    state=WorkItemState.READY,
                    wave=wave,
                    metadata={"handoff_to": item.handoff_to},
                )
            )
            ids.append(wid)
            if item.handoff_to:
                run.metadata["preferred_agent"] = item.handoff_to
        return ids

    def _compile_schedule_edges(
        self, run: TeamRun, wave_ids: list[str], coordination: CoordinationSpec
    ) -> None:
        deps = self.deps_for(run.id)
        board = self.board_for(run.id)
        existing = {(e.pred, e.succ, e.kind) for e in deps.edges}

        # Intra-wave role pipeline (institutional stage graph → FS edges).
        pipe = list(coordination.work_graph.role_pipeline or ())
        if pipe and wave_ids:
            by_role: dict[str, str] = {}
            for wid in wave_ids:
                item = board.get(wid)
                if item is None:
                    continue
                tags = [str(t) for t in (item.role_tags or []) if t]
                role = tags[0] if tags else ""
                if role and role not in by_role:
                    by_role[role] = wid
            for pred_role, succ_role in zip(pipe, pipe[1:]):
                pred_id = by_role.get(pred_role)
                succ_id = by_role.get(succ_role)
                if not pred_id or not succ_id:
                    continue
                key = (pred_id, succ_id, DepKind.FS)
                if key not in existing:
                    deps.add_edge(DepEdge(pred=pred_id, succ=succ_id, kind=DepKind.FS))
                    existing.add(key)

        if coordination.scheduling.mode != SchedulingMode.BARRIER:
            return
        # Inter-wave barrier: FS from every item in wave k to every item in wave k+1.
        waves: dict[int, list[str]] = {}
        for item in board.all_items():
            waves.setdefault(item.wave, []).append(item.id)
        ordered = [waves[k] for k in sorted(waves)]
        for edge in compile_barrier_waves(ordered):
            key = (edge.pred, edge.succ, edge.kind)
            if key not in existing:
                deps.add_edge(edge)
                existing.add(key)

    def _spawn_replica(self, run: TeamRun, parent: AgentSlot, item: WorkItem) -> AgentSlot | None:
        coordination = CoordinationSpec.from_run_metadata(run.metadata)
        max_rep = self._guard.effective_max_replicas(coordination)
        n_rep = sum(1 for a in run.agents if a.replica_of == parent.agent_key)
        if n_rep >= max_rep:
            return None
        key = f"{parent.agent_key}__r{n_rep + 1}_{uuid.uuid4().hex[:6]}"
        work_dir = Path(run.work_root) / key
        work_dir.mkdir(parents=True, exist_ok=True)
        replica = AgentSlot(
            agent_key=key,
            profile=dict(parent.profile),
            work_dir=str(work_dir),
            replica_of=parent.agent_key,
            task_memory_dir=str(work_dir / "memory"),
        )
        run.agents.append(replica)
        return replica

    def _process_merges(self, run: TeamRun) -> None:
        merges = Path(run.work_root) / "merges"
        if not merges.is_dir():
            return
        board = self.board_for(run.id)
        clerk = MergeClerk()
        for path in sorted(merges.glob("*_delta.json")):
            handled = path.with_suffix(".done")
            if handled.exists():
                continue
            delta = read_delta(path)
            parent = next((a for a in run.agents if a.agent_key == delta.replica_key), None)
            if parent is None or not parent.replica_of:
                continue
            main = next((a for a in run.agents if a.agent_key == parent.replica_of), None)
            if main is None:
                continue
            result = clerk.apply(delta, target_root=main.work_dir)
            if result.ok:
                handled.write_text(result.message, encoding="utf-8")
            else:
                mid = _new_id("merge")
                board.post(
                    WorkItem(
                        id=mid,
                        prompt=f"Resolve merge conflicts: {result.message}",
                        role_tags=[main.agent_key],
                        priority=100,
                        state=WorkItemState.BLOCKED,
                        error=result.message,
                        metadata={"conflicts": result.conflicts, "delta": str(path)},
                    )
                )
                handled.write_text("blocked", encoding="utf-8")

    async def tick(self, team_run_id: str) -> DriveResult:
        run = self._store.require(team_run_id)
        coordination = CoordinationSpec.from_run_metadata(run.metadata)
        board = self.board_for(run.id)
        deps = self.deps_for(run.id)
        now = datetime.now(UTC)
        run.updated_at = now

        if run.state == TeamRunState.PENDING:
            run.state = TeamRunState.RUNNING

        needs_plan = bool(run.metadata.pop("needs_round_plan", False)) or not board.all_items()
        if needs_plan and run.state == TeamRunState.RUNNING:
            plan = await self._coordinator.plan_round(run)
            if plan.needs_user_input:
                run.state = TeamRunState.WAITING_USER
                self._store.save(run)
                return DriveResult(run.id, run.state, 0, plan.rationale or "needs user")
            run.active_round = plan.round_index
            wave_ids = self._post_plan_to_board(run, plan.tasks, wave=plan.round_index)
            self._compile_schedule_edges(run, wave_ids, coordination)
            # Initially block items that aren't dep-ready
            deps.refresh_ready(board)
            # Flush after posting so observatory sees new wave cards before heartbeats finish.
            run.tasks = project_tasks(board, round_index=run.active_round)
            try:
                snapshot_team_run(run, board=board, deps=deps)
            except OSError:
                pass

        max_parallel = self._guard.effective_max_parallel(coordination)
        deps.refresh_ready(board)

        for a in run.agents:
            if a.state == AgentState.DONE:
                a.state = AgentState.IDLE

        ready_now = deps.ready_set(board)

        def _can_claim(agent: AgentSlot) -> bool:
            for item in ready_now:
                tags = item.role_tags or []
                if not tags:
                    return True
                role = str(agent.profile.get("role") or agent.agent_key)
                base = agent.replica_of or agent.agent_key
                if any(t in {agent.agent_key, base, role} for t in tags):
                    return True
            return False

        # Prefer idle agents who match ready work (avoid waking a finished role
        # that only rests while starving others under max_parallel=1).
        idle_matching = [a for a in run.agents if a.state == AgentState.IDLE and _can_claim(a)]
        idle_other = [
            a for a in run.agents if a.state == AgentState.IDLE and a not in idle_matching
        ]
        # Also wake RUNNING agents when urgent ready work exists (fork/preempt).
        urgent_ready = [i for i in ready_now if i.urgent]
        running_for_urgent = []
        if urgent_ready:
            running_for_urgent = [
                a
                for a in run.agents
                if a.state == AgentState.RUNNING
                and any(
                    (not i.role_tags)
                    or any(
                        t
                        in {
                            a.agent_key,
                            a.replica_of or a.agent_key,
                            str(a.profile.get("role") or a.agent_key),
                        }
                        for t in i.role_tags
                    )
                    for i in urgent_ready
                )
            ]
        idle_agents = (running_for_urgent + idle_matching + idle_other)[: max(1, max_parallel)]

        self._inboxes.setdefault(run.id, {})
        budget_left = self._guard.effective_max_total_rounds(coordination)
        if budget_left > 0:
            budget_left = max(0, budget_left - int(run.metadata.get("heartbeats_used") or 0))

        async def _hb(agent: AgentSlot):
            def spawn(parent: AgentSlot, item: WorkItem):
                return self._spawn_replica(run, parent, item)

            return await self._heartbeat.run_once(
                run=run,
                agent=agent,
                board=board,
                deps=deps,
                coordination=coordination,
                inbox=self.inbox_for(run.id, agent.agent_key),
                budget_rounds_left=budget_left,
                spawn_replica=spawn,
            )

        results = []
        if idle_agents:
            results = await asyncio.gather(*(_hb(a) for a in idle_agents))
            # If fork requested, immediately heartbeat the new replica
            for r in results:
                if r.spawned:
                    replica = next(a for a in run.agents if a.agent_key == r.spawned)
                    # Claim urgent item for replica
                    item = board.get(r.work_item_id) if r.work_item_id else None
                    if item and item.state == WorkItemState.READY:
                        await self._heartbeat.run_once(
                            run=run,
                            agent=replica,
                            board=board,
                            deps=deps,
                            coordination=coordination,
                            inbox=self.inbox_for(run.id, replica.agent_key),
                            budget_rounds_left=budget_left,
                        )

            for result in results:
                if result.decision.action.value == "escalate":
                    self._route_escalation(run, board, result, coordination)

        # After completions, promote waiting_deps→ready in the same tick so the
        # pipeline does not sit one tick behind (and the UI does not linger on waits).
        deps.refresh_ready(board)

        dispatched = sum(1 for r in results if r.work_item_id and r.completed)
        run.metadata["heartbeats_used"] = int(run.metadata.get("heartbeats_used") or 0) + len(
            results
        )

        self._process_merges(run)
        run.tasks = project_tasks(board, round_index=run.active_round)

        # Assess when board has no inflight ready/running (quiescent or all terminal)
        should_assess = False
        if board.all_items():
            inflight = board.by_state(
                WorkItemState.READY,
                WorkItemState.CLAIMED,
                WorkItemState.RUNNING,
                WorkItemState.SUSPENDED,
            )
            # Barrier template: also wait until current wave done
            if coordination.scheduling.mode == SchedulingMode.BARRIER:
                current = [i for i in board.all_items() if i.wave == run.active_round]
                should_assess = bool(current) and all(
                    i.state in {WorkItemState.DONE, WorkItemState.FAILED} for i in current
                )
            else:
                should_assess = not inflight

        if should_assess:
            aggregation = apply_aggregation(run, coordination.aggregation)
            assessment = await self._coordinator.assess_round(run)
            if assessment.preferred_agent:
                run.metadata["preferred_agent"] = assessment.preferred_agent
            if assessment.next_round_hint:
                run.metadata["next_round_hint"] = assessment.next_round_hint
            else:
                run.metadata.pop("next_round_hint", None)
            reason_code = str(getattr(assessment, "reason_code", "") or "")
            if reason_code:
                run.metadata["last_assessment_reason"] = reason_code
            else:
                run.metadata.pop("last_assessment_reason", None)
            if aggregation is not None:
                run.metadata["last_aggregation"] = aggregation

            # Mechanical envelope faults: retry a few times, then human gate —
            # never hard-stop the campaign for a bad JSON reply.
            recover_codes = {"illegal_assessment", "promote_failed"}
            if reason_code in recover_codes:
                streak = int(run.metadata.get("mechanical_fault_streak") or 0) + 1
                run.metadata["mechanical_fault_streak"] = streak
            else:
                run.metadata.pop("mechanical_fault_streak", None)

            max_rounds = self._guard.effective_max_total_rounds(coordination)
            budget_hit = (
                assessment.decision == AssessmentDecision.CONTINUE
                and max_rounds > 0
                and run.active_round >= max_rounds
            )
            mechanical_escalate = (
                reason_code in recover_codes
                and int(run.metadata.get("mechanical_fault_streak") or 0) >= 3
            )
            gate = (
                coordination.human_gate
                and assessment.decision == AssessmentDecision.COMPLETED
                and not run.metadata.get("human_gate_cleared")
            )
            if mechanical_escalate:
                run.state = TeamRunState.WAITING_USER
                run.metadata["gate_reason"] = "assessor_envelope_repair"
                run.metadata["human_gate_pending"] = True
            elif gate:
                run.state = TeamRunState.WAITING_USER
                run.metadata["gate_reason"] = "human_gate"
                run.metadata["human_gate_pending"] = True
            elif budget_hit:
                # A declared round count is an initial scheduling scale, not a
                # scientific stop rule.  Continue while the assessor still
                # reports CONTINUE; it alone may eventually establish that
                # the task has converged or is no longer informative.
                stopping = dict(coordination.stopping.to_dict())
                prior = int(
                    stopping.get("max_total_rounds") or stopping.get("max_iterations") or 0
                )
                extra = max(1, prior)
                new_ceiling = prior + extra if prior else max(1, run.active_round + 1)
                if int(stopping.get("max_total_rounds") or 0) > 0:
                    stopping["max_total_rounds"] = new_ceiling
                stopping["max_iterations"] = new_ceiling
                coordination = CoordinationSpec.from_dict(
                    {**coordination.to_dict(), "stopping": stopping}
                )
                run.metadata["coordination"] = coordination.to_dict()
                extensions = list(run.metadata.get("automatic_iteration_extensions") or [])
                extensions.append(
                    {
                        "prior_ceiling": prior,
                        "new_ceiling": new_ceiling,
                        "at_round": int(run.active_round),
                        "reason": "assessor_continue",
                    }
                )
                run.metadata["automatic_iteration_extensions"] = extensions
                run.metadata["needs_round_plan"] = True
            elif assessment.decision == AssessmentDecision.COMPLETED:
                run.state = TeamRunState.COMPLETED
                run.artifacts = assessment.artifact_plan or [
                    {"kind": "team_summary", "content": assessment.summary}
                ]
            elif assessment.decision == AssessmentDecision.FAILED:
                run.state = TeamRunState.FAILED
            elif assessment.decision == AssessmentDecision.WAITING_USER:
                run.state = TeamRunState.WAITING_USER
            else:
                run.metadata["needs_round_plan"] = True

        self._store.save(run)
        try:
            snapshot_team_run(run, board=board, deps=deps)
        except OSError:
            pass
        return DriveResult(
            team_run_id=run.id,
            state=run.state,
            dispatched=dispatched,
            message="tick complete",
        )

    async def drive(self, team_run_id: str, *, max_ticks: int = 32) -> DriveResult:
        result = DriveResult(team_run_id, TeamRunState.PENDING, 0, "idle")
        for _ in range(max(1, max_ticks)):
            result = await self.tick(team_run_id)
            if is_terminal(result.state) or result.state == TeamRunState.WAITING_USER:
                break
            run = self._store.require(team_run_id)
            board = self.board_for(team_run_id)
            if run.state == TeamRunState.RUNNING and not board.has_inflight():
                if not run.metadata.get("needs_round_plan"):
                    run.metadata["needs_round_plan"] = True
                    self._store.save(run)
        return result

    async def drive_until_idle(self, team_run_id: str, *, max_ticks: int = 32) -> DriveResult:
        return await self.drive(team_run_id, max_ticks=max_ticks)

    def require(self, team_run_id: str) -> TeamRun:
        return self._store.require(team_run_id)

    def extend_budget(
        self,
        team_run_id: str,
        *,
        extra_iterations: int | None = None,
        unlimited: bool = False,
        note: str = "",
    ) -> TeamRun:
        """Clear budget_exhausted and raise (or remove) the round ceiling.

        Called after a human budget-gate grant so the mission tick loop can
        continue without restarting the campaign process.
        """
        run = self._store.require(team_run_id)
        coord = dict(run.metadata.get("coordination") or {})
        stopping = dict(coord.get("stopping") or {})
        prior = int(stopping.get("max_total_rounds") or stopping.get("max_iterations") or 0)
        if unlimited:
            stopping["max_iterations"] = 0
            stopping["max_total_rounds"] = 0
            new_ceil = 0
        else:
            extra = max(1, int(extra_iterations or 0))
            floor = int(run.active_round) + extra
            new_ceil = max(prior + extra, floor) if prior > 0 else floor
            if int(stopping.get("max_total_rounds") or 0) > 0:
                stopping["max_total_rounds"] = new_ceil
            stopping["max_iterations"] = new_ceil
        coord["stopping"] = stopping
        run.metadata["coordination"] = coord
        gate_reason = str(run.metadata.get("gate_reason") or "")
        history = list(run.metadata.get("budget_extensions") or [])
        history.append(
            {
                "prior_ceiling": prior,
                "new_ceiling": new_ceil,
                "unlimited": bool(unlimited),
                "extra_iterations": 0 if unlimited else max(1, int(extra_iterations or 0)),
                "note": note,
                "at_round": int(run.active_round),
            }
        )
        run.metadata["budget_extensions"] = history
        run.metadata.pop("budget_exhausted", None)
        run.metadata.pop("gate_reason", None)
        run.metadata.pop("human_gate_pending", None)
        run.metadata.pop("mechanical_fault_streak", None)
        # Observatory grant path must clear the institutional human_gate the same
        # way scripted matrix tests do — otherwise COMPLETED re-parks forever.
        if gate_reason == "human_gate":
            run.metadata["human_gate_cleared"] = True
        if gate_reason in {"worker_escalate", "worker_retry_exhausted"}:
            board = self.board_for(team_run_id)
            for item in board.by_state(WorkItemState.SUSPENDED):
                item.metadata["retry_count"] = 0
                item.metadata["retry_hint"] = (
                    f"Human gate granted continuation. Operator context: {note}"
                )
                board.resume(item.id)
            run.metadata.pop("worker_escalation", None)
            run.metadata.pop("needs_round_plan", None)
        else:
            run.metadata["needs_round_plan"] = True
        run.state = TeamRunState.RUNNING
        self._store.save(run)
        try:
            board = self.board_for(team_run_id)
            deps = self.deps_for(team_run_id)
            snapshot_team_run(run, board=board, deps=deps)
        except OSError:
            pass
        return run
