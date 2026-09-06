"""Campaign layer: budgeted outer loop over missions.

The campaign engine knows nothing about research stages. It holds a standing
goal, a budget clock, a knowledge base and a planner port. What looks like a
"scientific method" is whatever mission sequence the planner emits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from sciteam.kb import CampaignKB
from sciteam.mission import MissionOutcome, MissionRunner, MissionSpec
from sciteam.trace import CampaignTrace


@dataclass(frozen=True)
class CampaignBudget:
    max_missions: int = 0
    max_wall_clock_seconds: int = 0
    max_llm_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CampaignBudget:
        data = data or {}
        return cls(
            max_missions=int(data.get("max_missions") or 0),
            max_wall_clock_seconds=int(data.get("max_wall_clock_seconds") or 0),
            max_llm_tokens=int(data.get("max_llm_tokens") or 0),
        )


class BudgetClock:
    """Tracks global consumption. Zero limits mean unlimited."""

    def __init__(self, budget: CampaignBudget) -> None:
        self.budget = budget
        self.missions_run = 0
        self.llm_tokens_used = 0
        self._started = time.monotonic()

    @property
    def wall_clock_seconds(self) -> float:
        return time.monotonic() - self._started

    def charge_mission(self) -> None:
        self.missions_run += 1

    def charge_tokens(self, tokens: int) -> None:
        self.llm_tokens_used += max(0, int(tokens))

    def exhausted(self) -> str:
        """Returns the name of the first exhausted dimension, or empty string."""
        b = self.budget
        if 0 < b.max_missions <= self.missions_run:
            return "max_missions"
        if 0 < b.max_wall_clock_seconds <= self.wall_clock_seconds:
            return "max_wall_clock_seconds"
        if 0 < b.max_llm_tokens <= self.llm_tokens_used:
            return "max_llm_tokens"
        return ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "missions_run": self.missions_run,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "llm_tokens_used": self.llm_tokens_used,
            "limits": {
                "max_missions": self.budget.max_missions,
                "max_wall_clock_seconds": self.budget.max_wall_clock_seconds,
                "max_llm_tokens": self.budget.max_llm_tokens,
            },
        }


@dataclass(frozen=True)
class PlannerDecision:
    action: str  # "run_mission" | "stop"
    mission: MissionSpec | None = None
    reason: str = ""
    reentry: bool = False
    reentry_target: str = ""


@runtime_checkable
class CampaignPlanner(Protocol):
    def next_mission(
        self,
        *,
        last_outcome: MissionOutcome | None,
        kb: CampaignKB,
        clock: BudgetClock,
    ) -> PlannerDecision: ...


@dataclass
class _StepState:
    step: dict[str, Any]
    attempts: int = 0
    succeeded: bool = False


class ScriptedPlaybookPlanner:
    """Deterministic planner driven by a YAML playbook.

    The playbook is policy, not engine: an ordered list of mission steps with
    per-step failure rules (retry in place, re-enter an earlier step, continue,
    or abort). An optional `finalizer` mission runs on abort/exhaustion so the
    campaign always ends with an honest artifact.
    """

    def __init__(self, playbook: dict[str, Any]) -> None:
        steps = playbook.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("playbook.steps must be a non-empty list")
        self._steps: list[_StepState] = []
        for index, raw in enumerate(steps):
            if not isinstance(raw, dict) or not isinstance(raw.get("mission"), dict):
                raise ValueError(f"playbook.steps[{index}] must contain a mission mapping")
            MissionSpec.from_dict(raw["mission"])  # validate eagerly
            self._steps.append(_StepState(step=raw))
        self._finalizer: dict[str, Any] | None = None
        raw_final = playbook.get("finalizer")
        if isinstance(raw_final, dict) and isinstance(raw_final.get("mission"), dict):
            MissionSpec.from_dict(raw_final["mission"])
            self._finalizer = raw_final
        self._cursor = 0
        self._finalizer_issued = False
        self._aborting = False
        self._pending_reentry: str = ""

    @classmethod
    def from_yaml(cls, path: Path | str) -> ScriptedPlaybookPlanner:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: playbook must be a mapping")
        return cls(raw)

    def _step_id(self, state: _StepState) -> str:
        return str(state.step["mission"]["id"])

    def _spec_for(self, state: _StepState) -> MissionSpec:
        state.attempts += 1
        data = dict(state.step["mission"])
        if state.attempts > 1:
            data = dict(data)
            data["id"] = f"{data['id']}_a{state.attempts}"
        return MissionSpec.from_dict(data)

    def _index_of(self, step_id: str) -> int | None:
        for i, state in enumerate(self._steps):
            if self._step_id(state) == step_id:
                return i
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
        del kb
        if self._finalizer_issued:
            return PlannerDecision(action="stop", reason="finalizer already ran")
        if self._aborting:
            return self._issue_finalizer("aborted by failure policy")
        exhausted = clock.exhausted()
        if exhausted:
            return self._issue_finalizer(f"campaign budget exhausted: {exhausted}")

        reentry = bool(self._pending_reentry)
        reentry_target = self._pending_reentry
        self._pending_reentry = ""

        if self._cursor >= len(self._steps):
            return PlannerDecision(action="stop", reason="playbook complete")
        state = self._steps[self._cursor]
        return PlannerDecision(
            action="run_mission",
            mission=self._spec_for(state),
            reason=f"playbook step {self._cursor + 1}/{len(self._steps)}",
            reentry=reentry,
            reentry_target=reentry_target,
        )

    def observe(self, outcome: MissionOutcome) -> None:
        """Feed the finished mission back so the cursor / failure policy moves."""
        if self._finalizer_issued or self._cursor >= len(self._steps):
            return
        state = self._steps[self._cursor]
        base_id = self._step_id(state)
        if not (outcome.mission_id == base_id or outcome.mission_id.startswith(f"{base_id}_a")):
            return
        if outcome.succeeded:
            state.succeeded = True
            self._cursor += 1
            return
        policy = state.step.get("on_fail") or {}
        action = str(policy.get("action") or "abort")
        max_times = int(policy.get("max_times") or 1)
        if action == "retry" and state.attempts < max_times + 1:
            return  # same cursor; next call re-issues with a new attempt id
        if action == "reenter":
            target = str(policy.get("target") or "")
            index = self._index_of(target)
            if index is not None:
                target_state = self._steps[index]
                if target_state.attempts < max_times + 1:
                    for later in self._steps[index:]:
                        later.succeeded = False
                    self._cursor = index
                    self._pending_reentry = target
                    return
            self._aborting = True
            return
        if action == "continue":
            self._cursor += 1
            return
        self._aborting = True


@dataclass
class CampaignResult:
    campaign_id: str
    stop_reason: str
    outcomes: list[MissionOutcome] = field(default_factory=list)

    @property
    def succeeded_missions(self) -> int:
        return sum(1 for o in self.outcomes if o.succeeded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "stop_reason": self.stop_reason,
            "missions": [o.to_dict() for o in self.outcomes],
            "succeeded_missions": self.succeeded_missions,
        }


class Campaign:
    """Outer loop: planner proposes missions, runner executes, KB/trace record."""

    def __init__(
        self,
        *,
        campaign_id: str,
        goal: str,
        planner: CampaignPlanner,
        runner: MissionRunner,
        kb: CampaignKB,
        trace: CampaignTrace,
        budget: CampaignBudget | None = None,
        token_meter: Any = None,
    ) -> None:
        """`token_meter` is an optional zero-arg callable returning cumulative
        token consumption; the clock charges deltas after each mission."""
        self.campaign_id = campaign_id
        self.goal = goal
        self._planner = planner
        self._runner = runner
        self._kb = kb
        self._trace = trace
        self._clock = BudgetClock(budget or CampaignBudget())
        self._token_meter = token_meter
        self._tokens_charged = 0

    @property
    def clock(self) -> BudgetClock:
        return self._clock

    @property
    def kb(self) -> CampaignKB:
        return self._kb

    @property
    def trace(self) -> CampaignTrace:
        return self._trace

    async def run(self, *, max_missions_safety: int = 256) -> CampaignResult:
        self._trace.record(
            "campaign_started",
            campaign_id=self.campaign_id,
            goal=self.goal,
            budget=self._clock.snapshot()["limits"],
        )
        outcomes: list[MissionOutcome] = []
        stop_reason = "planner stopped"
        for _ in range(max(1, max_missions_safety)):
            decision = self._planner.next_mission(
                last_outcome=outcomes[-1] if outcomes else None,
                kb=self._kb,
                clock=self._clock,
            )
            self._trace.record(
                "planner_decision",
                action=decision.action,
                reason=decision.reason,
                reentry=decision.reentry,
                reentry_target=decision.reentry_target,
                mission_id=decision.mission.id if decision.mission else "",
                paradigm=decision.mission.paradigm if decision.mission else "",
                budget=self._clock.snapshot(),
            )
            if decision.action != "run_mission" or decision.mission is None:
                stop_reason = decision.reason or "planner stopped"
                break

            spec = decision.mission
            self._trace.record(
                "mission_started",
                mission_id=spec.id,
                kind=spec.kind,
                paradigm=spec.paradigm,
                exit_contract=spec.exit_contract,
                skills_allowlist=list(spec.skills_allowlist),
            )
            outcome = await self._runner.run(spec)
            self._clock.charge_mission()
            if self._token_meter is not None:
                total = int(self._token_meter())
                self._clock.charge_tokens(total - self._tokens_charged)
                self._tokens_charged = total
            outcomes.append(outcome)
            self._trace.record(
                "mission_finished",
                mission_id=outcome.mission_id,
                status=outcome.status,
                paradigm=outcome.paradigm,
                contract_ok=outcome.contract_ok,
                contract_errors=outcome.contract_errors,
                rounds_used=outcome.rounds_used,
                artifact_path=outcome.artifact_path,
            )
            self._kb.append(
                "mission_outcome",
                outcome.to_dict(),
                mission_id=outcome.mission_id,
            )
            observe = getattr(self._planner, "observe", None)
            if callable(observe):
                observe(outcome)
            snapshot = getattr(self._planner, "plan_snapshot", None)
            if callable(snapshot):
                state = snapshot()
                self._trace.record(
                    "planner_state",
                    nodes=state.get("nodes", []),
                    feedback_events=state.get("feedback_events", []),
                    revisions_used=state.get("revisions_used", 0),
                )
        else:
            stop_reason = "safety mission cap reached"

        self._trace.record(
            "campaign_finished",
            campaign_id=self.campaign_id,
            stop_reason=stop_reason,
            budget=self._clock.snapshot(),
            missions=len(outcomes),
        )
        return CampaignResult(
            campaign_id=self.campaign_id, stop_reason=stop_reason, outcomes=outcomes
        )
