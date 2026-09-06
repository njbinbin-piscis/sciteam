"""Adaptive, DAG-driven campaign planner.

A ResearchPlan is *data*: a set of mission nodes with dependencies and per-node
failure policy. This planner traverses the DAG, and — between missions — may
apply revisions returned by an injected reviser policy (add catalog missions in
light of new evidence). The engine itself branches on nothing research-specific:
node ids, dependencies and revisions are opaque. This is the mechanism that lets
"even the research plan is a policy" hold without any engine change (E0=0).

Locked node ids (frozen endpoints, sealed-holdout audit, statistical budget)
can never be replaced or removed by a revision; revisions may only add nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sciteam.campaign import BudgetClock, PlannerDecision
from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome, MissionSpec


@dataclass
class _NodeState:
    node_id: str
    mission: dict[str, Any]
    deps: tuple[str, ...] = ()
    on_fail: dict[str, Any] = field(default_factory=dict)
    origin: str = "plan"
    attempts: int = 0
    resolved: bool = False
    succeeded: bool = False
    superseded: bool = False
    superseded_by: str = ""
    reopen_reason: str = ""


@runtime_checkable
class PlanReviser(Protocol):
    def revise(
        self,
        *,
        kb: CampaignKB | None,
        completed: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class CompositeReviser:
    """Fan a single `_maybe_revise()` call out to several revisers in order.

    `AdaptiveCampaignPlanner` only takes one `reviser=`. A canonical run
    typically needs at least two independent, unrelated reviser policies at
    once (e.g. a science-loop reviser reacting to a negative frozen eval,
    and a `sciteam.audit_veto.VetoUpstreamReviser` reacting to an oversight
    seat's veto) — this composes them without either policy knowing the
    other exists. Order matters only if two revisers would otherwise target
    the same node in the same tick; callers should list higher-priority
    revisers first.
    """

    def __init__(self, revisers: list[PlanReviser]) -> None:
        self._revisers = list(revisers)

    def revise(
        self,
        *,
        kb: CampaignKB | None,
        completed: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for reviser in self._revisers:
            out.extend(reviser.revise(kb=kb, completed=completed, nodes=nodes) or [])
        return out


class AdaptiveCampaignPlanner:
    def __init__(
        self,
        plan: dict[str, Any],
        *,
        reviser: PlanReviser | None = None,
        max_revisions: int = 0,
        locked_ids: tuple[str, ...] = (),
        finalizer: dict[str, Any] | None = None,
    ) -> None:
        nodes = plan.get("missions")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("plan.missions must be a non-empty list")
        self._nodes: list[_NodeState] = []
        self._ids: set[str] = set()
        for raw in nodes:
            self._add_node(raw, origin="plan")
        self._validate_deps()
        self._reviser = reviser
        self._max_revisions = max(0, int(max_revisions))
        self._revisions_used = 0
        self._locked = set(locked_ids)
        self._finalizer = finalizer if (finalizer and finalizer.get("mission")) else None
        self._finalizer_issued = False
        self._aborting = False
        self._completed: list[dict[str, Any]] = []
        self._feedback_events: list[dict[str, Any]] = []
        self._kb: CampaignKB | None = None

    # --- construction -------------------------------------------------------
    def _add_node(self, raw: dict[str, Any], *, origin: str) -> None:
        if not isinstance(raw, dict) or not isinstance(raw.get("mission"), dict):
            raise ValueError("plan node must contain a mission mapping")
        MissionSpec.from_dict(raw["mission"])  # eager validation
        node_id = str(raw["mission"]["id"])
        if node_id in self._ids:
            raise ValueError(f"duplicate mission id: {node_id}")
        self._ids.add(node_id)
        self._nodes.append(
            _NodeState(
                node_id=node_id,
                mission=dict(raw["mission"]),
                deps=tuple(str(d) for d in (raw.get("deps") or [])),
                on_fail=dict(raw.get("on_fail") or {}),
                origin=origin,
            )
        )

    def _validate_deps(self) -> None:
        for node in self._nodes:
            for dep in node.deps:
                if dep not in self._ids:
                    raise ValueError(f"node {node.node_id} depends on unknown {dep}")

    def _find(self, node_id: str) -> _NodeState | None:
        for node in self._nodes:
            if node.node_id == node_id:
                return node
        return None

    def _base_id(self, mission_id: str) -> str:
        return mission_id.rsplit("_a", 1)[0]

    # --- planning -----------------------------------------------------------
    def _spec_for(self, node: _NodeState) -> MissionSpec:
        node.attempts += 1
        data = dict(node.mission)
        if node.attempts > 1:
            data = dict(data)
            data["id"] = f"{data['id']}_a{node.attempts}"
        return MissionSpec.from_dict(data)

    def _deps_resolved(self, node: _NodeState) -> bool:
        for dep in node.deps:
            target = self._find(dep)
            if target is None or not target.resolved:
                return False
        return True

    def _ready_node(self) -> _NodeState | None:
        for node in self._nodes:
            if node.resolved or node.superseded:
                continue
            if self._deps_resolved(node):
                return node
        return None

    def _issue_finalizer(self, reason: str) -> PlannerDecision:
        if self._finalizer is None or self._finalizer_issued:
            return PlannerDecision(action="stop", reason=reason)
        self._finalizer_issued = True
        return PlannerDecision(
            action="run_mission",
            mission=MissionSpec.from_dict(self._finalizer["mission"]),
            reason=f"finalizer: {reason}",
        )

    def next_mission(
        self,
        *,
        last_outcome: MissionOutcome | None,
        kb: CampaignKB,
        clock: BudgetClock,
    ) -> PlannerDecision:
        del last_outcome
        self._kb = kb
        if self._finalizer_issued:
            return PlannerDecision(action="stop", reason="finalizer already ran")
        if self._aborting:
            return self._issue_finalizer("aborted by failure policy")
        exhausted = clock.exhausted()
        if exhausted:
            return self._issue_finalizer(f"campaign budget exhausted: {exhausted}")
        node = self._ready_node()
        if node is None:
            if all(n.resolved for n in self._nodes):
                return PlannerDecision(action="stop", reason="plan complete")
            return self._issue_finalizer("plan deadlocked (unmet dependencies)")
        remaining = sum(1 for n in self._nodes if not n.resolved)
        return PlannerDecision(
            action="run_mission",
            mission=self._spec_for(node),
            reason=f"plan node {node.node_id} ({remaining} unresolved, origin={node.origin})",
        )

    def hydrate_from_run_dir(self, run_dir: Path | str) -> int:
        """Mark plan nodes resolved from on-disk mission artifacts (process restart).

        A node is treated as succeeded when ``missions/<id>/<exit_contract>.json``
        exists and is non-trivial. In-progress / budget-exhausted missions without
        that artifact are left unresolved so the next process resumes there.
        """
        missions_root = Path(run_dir) / "missions"
        if not missions_root.is_dir():
            return 0
        n = 0
        for node in self._nodes:
            if node.resolved:
                continue
            contract = str(node.mission.get("exit_contract") or "").strip()
            if not contract:
                continue
            artifact = missions_root / node.node_id / f"{contract}.json"
            if not artifact.is_file() or artifact.stat().st_size < 3:
                continue
            node.succeeded = True
            node.resolved = True
            node.attempts = max(1, node.attempts)
            self._completed.append(
                {
                    "mission_id": node.node_id,
                    "status": "completed",
                    "contract_ok": True,
                    "artifact_path": str(artifact),
                    "hydrated": True,
                }
            )
            n += 1
        return n

    def observe(self, outcome: MissionOutcome) -> None:
        if self._finalizer_issued:
            return
        node = self._find(self._base_id(outcome.mission_id))
        if node is None:
            return
        self._completed.append(outcome.to_dict())
        if outcome.succeeded:
            node.succeeded = True
            node.resolved = True
            self._maybe_revise()
            return
        self._apply_failure(node)

    def _apply_failure(self, node: _NodeState) -> None:
        policy = node.on_fail
        action = str(policy.get("action") or "abort")
        max_times = int(policy.get("max_times") or 1)
        if action == "retry" and node.attempts < max_times + 1:
            return  # re-issued with next attempt id
        if action == "reenter":
            target = self._find(str(policy.get("target") or ""))
            if target is not None and target.attempts < max_times + 1:
                self._reset_from(target)
                return
            self._aborting = True
            return
        if action == "continue":
            node.resolved = True
            self._maybe_revise()
            return
        self._aborting = True

    def _reset_from(self, target: _NodeState) -> None:
        """Reset the target and every node that (transitively) depends on it."""
        blocked = {target.node_id}
        changed = True
        while changed:
            changed = False
            for node in self._nodes:
                if node.node_id in blocked:
                    continue
                if any(d in blocked for d in node.deps):
                    blocked.add(node.node_id)
                    changed = True
        for node in self._nodes:
            if node.node_id in blocked and node.node_id not in self._locked:
                node.resolved = False
                node.succeeded = False

    def reopen_upstream(
        self,
        *,
        target: str,
        reason_code: str,
        requested_by: str = "",
    ) -> bool:
        """Reopen a declared upstream node and all transitive dependents."""
        node = self._find(str(target))
        if node is None or node.node_id in self._locked:
            return False
        self._reset_from(node)
        node.reopen_reason = str(reason_code or "downstream_counterexample")
        self._feedback_events.append(
            {
                "action": "REOPEN_UPSTREAM",
                "target": node.node_id,
                "reason_code": node.reopen_reason,
                "requested_by": str(requested_by),
            }
        )
        return True

    def supersede(
        self,
        *,
        target: str,
        replacement: str,
        reason_code: str,
    ) -> bool:
        """Mark a prior artifact/node obsolete while preserving its provenance."""
        old = self._find(str(target))
        new = self._find(str(replacement))
        if old is None or new is None or old.node_id in self._locked:
            return False
        old.superseded = True
        old.superseded_by = new.node_id
        old.resolved = True
        for node in self._nodes:
            if old.node_id in node.deps:
                node.deps = tuple(new.node_id if dep == old.node_id else dep for dep in node.deps)
        event = {
            "action": "SUPERSEDE",
            "target": old.node_id,
            "replacement": new.node_id,
            "reason_code": str(reason_code or "superseded"),
        }
        self._feedback_events.append(event)
        for completed in reversed(self._completed):
            if self._base_id(str(completed.get("mission_id") or "")) != old.node_id:
                continue
            artifact_raw = str(completed.get("artifact_path") or "")
            artifact = Path(artifact_raw) if artifact_raw else None
            if artifact is not None and artifact.is_file():
                sidecar = artifact.with_suffix(artifact.suffix + ".superseded.json")
                try:
                    import json

                    sidecar.write_text(
                        json.dumps(event, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            break
        return True

    def _maybe_revise(self) -> None:
        if self._reviser is None or self._revisions_used >= self._max_revisions:
            return
        additions = self._reviser.revise(
            kb=self._kb,
            completed=list(self._completed),
            nodes=[self._node_summary(n) for n in self._nodes],
        )
        if not additions:
            return
        rewire_to = ""
        package_id = "m_package"
        applied = 0
        actions: list[dict[str, Any]] = []
        for raw in additions or []:
            if str((raw or {}).get("action") or "").upper() in {
                "REOPEN_UPSTREAM",
                "SUPERSEDE",
            }:
                actions.append(dict(raw))
                continue
            mission = (raw or {}).get("mission") or {}
            node_id = str(mission.get("id") or "")
            if not node_id or node_id in self._ids or node_id in self._locked:
                continue
            try:
                self._add_node(raw, origin="revision")
            except ValueError:
                continue
            applied += 1
            if raw.get("rewire_package_to"):
                rewire_to = str(raw["rewire_package_to"])
            if raw.get("package_id"):
                package_id = str(raw["package_id"])
        if not applied and not actions:
            return
        self._revisions_used += 1
        if rewire_to:
            self._rewire_package_deps(package_id, rewire_to)
        for action in actions:
            kind = str(action.get("action") or "").upper()
            if kind == "REOPEN_UPSTREAM":
                self.reopen_upstream(
                    target=str(action.get("target") or ""),
                    reason_code=str(action.get("reason_code") or ""),
                    requested_by=str(action.get("requested_by") or "reviser"),
                )
            elif kind == "SUPERSEDE":
                self.supersede(
                    target=str(action.get("target") or ""),
                    replacement=str(action.get("replacement") or ""),
                    reason_code=str(action.get("reason_code") or ""),
                )

    def _rewire_package_deps(self, package_id: str, new_dep: str) -> None:
        """Point an unresolved package node at the latest science build."""
        pkg = self._find(package_id)
        if pkg is None or pkg.resolved:
            return
        if new_dep not in self._ids:
            return
        pkg.deps = (new_dep,)

    def _node_summary(self, node: _NodeState) -> dict[str, Any]:
        return {
            "id": node.node_id,
            "deps": list(node.deps),
            "resolved": node.resolved,
            "succeeded": node.succeeded,
            "origin": node.origin,
            "superseded": node.superseded,
            "superseded_by": node.superseded_by,
            "reopen_reason": node.reopen_reason,
        }

    def plan_snapshot(self) -> dict[str, Any]:
        """Introspection for the trace / observatory."""
        return {
            "nodes": [self._node_summary(n) for n in self._nodes],
            "revisions_used": self._revisions_used,
            "max_revisions": self._max_revisions,
            "locked": sorted(self._locked),
            "feedback_events": list(self._feedback_events),
        }
