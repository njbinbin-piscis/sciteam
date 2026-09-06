"""Team coordinators: plan production seats; assess via AssessorPort."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sciteam.assessment_facts import build_assessment_facts
from sciteam.coordination import CoordinationSpec, StoppingKind
from sciteam.models import (
    AssessmentDecision,
    RoundAssessment,
    RoundPlan,
    RoundTask,
    TaskState,
    TeamRun,
)
from sciteam.round_assessor import (
    AssessorPort,
    ScriptedAssessor,
    assert_assessor_authorized,
    envelope_approves_promote,
    facts_only_verdict,
    parse_assessor_envelope,
)

_LAB_ASSETS = Path(__file__).resolve().parents[1] / "assets"


@runtime_checkable
class TeamCoordinator(Protocol):
    async def plan_round(self, run: TeamRun) -> RoundPlan: ...

    async def assess_round(self, run: TeamRun) -> RoundAssessment: ...


def _may_assess(profile: dict[str, Any]) -> bool:
    if profile.get("may_assess_round") is True:
        return True
    auth = profile.get("authority") or []
    return isinstance(auth, (list, tuple)) and "may_assess_round" in auth


def _load_task_brief(profile: dict[str, Any], *, assets_root: Path) -> str:
    rel = str(profile.get("task_brief") or "").strip()
    if not rel:
        return ""
    path = Path(rel)
    if not path.is_absolute():
        path = assets_root / rel
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _predecessor_digest(run: TeamRun, *, role: str, pipeline: list[str]) -> str:
    """Summarize same-wave done results from earlier pipeline roles."""
    if role not in pipeline:
        return ""
    idx = pipeline.index(role)
    if idx <= 0:
        return ""
    wanted = set(pipeline[:idx])
    wave = int(run.active_round or 0)
    chunks: list[str] = []
    for task in run.tasks:
        if int(task.round_index or 0) != wave:
            continue
        if task.agent_key not in wanted:
            continue
        if task.state.value not in {"done", "failed"}:
            continue
        body = (task.result or task.error or "")[:1200]
        chunks.append(f"### prior:{task.agent_key} ({task.state.value})\n{body}")
    if not chunks:
        return ""
    return "# Predecessor outputs (consume these; do not ignore)\n" + "\n\n".join(chunks)


def _assets_root(run: TeamRun) -> Path:
    meta = run.metadata or {}
    raw = meta.get("assets_root")
    if raw:
        return Path(str(raw))
    return _LAB_ASSETS


class ScriptedCoordinator:
    """Plans one task per *production* agent (skips may_assess seats)."""

    async def plan_round(self, run: TeamRun) -> RoundPlan:
        from sciteam.coordination import CoordinationSpec

        round_index = run.active_round + 1
        coord = CoordinationSpec.from_run_metadata(run.metadata)
        pipeline = list(coord.work_graph.role_pipeline or ())
        assets = _assets_root(run)
        tasks: list[RoundTask] = []
        for agent in run.agents:
            if _may_assess(agent.profile):
                continue
            role = str(agent.profile.get("role") or agent.agent_key)
            brief = _load_task_brief(agent.profile, assets_root=assets)
            stage = brief or (
                f"# YOUR JOB THIS TURN ({role})\n"
                f"Execute seat duty for role={role}. See role prompt."
            )
            pred = _predecessor_digest(run, role=role, pipeline=pipeline)
            parts = [
                stage,
                f"# Seat\n{agent.agent_key} ({agent.profile.get('name') or role})",
                f"# Campaign topic (context only)\n{run.goal}",
            ]
            if pipeline:
                parts.append(f"# Pipeline position\nYou are «{role}» in: " + " → ".join(pipeline))
            if pred:
                parts.append(pred)
            tasks.append(RoundTask(agent_key=agent.agent_key, prompt="\n\n".join(parts)))
        if not tasks:
            raise ValueError("plan_round: no production seats (all agents have may_assess_round)")
        return RoundPlan(
            round_index=max(1, round_index),
            tasks=tasks,
            rationale=(
                "scripted: one task per production agent"
                + (f"; pipeline={pipeline}" if pipeline else "")
            ),
        )

    async def assess_round(self, run: TeamRun) -> RoundAssessment:
        """Mechanical wait until the active wave's production tasks are terminal."""
        if not run.tasks:
            return RoundAssessment(
                decision=AssessmentDecision.CONTINUE,
                summary="no tasks yet",
                reason_code="",
            )
        current = [t for t in run.tasks if t.round_index == run.active_round]
        if not current:
            return RoundAssessment(
                decision=AssessmentDecision.CONTINUE,
                summary="no tasks in active round",
                reason_code="",
            )
        if any(t.state not in {TaskState.DONE, TaskState.FAILED} for t in current):
            return RoundAssessment(
                decision=AssessmentDecision.CONTINUE,
                summary="tasks still in progress",
                reason_code="",
            )
        # Quiescent signal for ContractCoordinator; not a team completion.
        done = [t for t in current if t.state == TaskState.DONE]
        return RoundAssessment(
            decision=AssessmentDecision.COMPLETED,
            summary="\n\n".join(t.result for t in done if t.result),
            artifact_plan=[{"kind": "team_summary", "content": "wave quiescent"}],
            reason_code="",
        )


def _try_promote_draft(metadata: dict[str, Any], artifact_path: str) -> Path | None:
    """Mechanical promote when an assessor envelope approves it."""
    exit_contract = str(metadata.get("exit_contract") or "").strip()
    if not exit_contract:
        return None
    dest = Path(artifact_path)
    schemas_dir = metadata.get("schemas_dir")
    validator = None
    if schemas_dir:
        try:
            from sciteam.mission import ContractValidator

            validator = ContractValidator(schemas_dir)
        except Exception:  # noqa: BLE001 — promotion is best-effort
            validator = None
    globs = metadata.get("draft_globs")
    glob_list = [str(x) for x in globs] if isinstance(globs, (list, tuple)) else None
    try:
        from sciteam.exit_artifact import promote_draft_to_exit

        return promote_draft_to_exit(
            mission_dir=dest.parent,
            exit_contract=exit_contract,
            artifact_path=dest,
            validator=validator,
            globs=glob_list,
        )
    except (FileNotFoundError, ValueError, OSError):
        return None


def _persist_json(run: TeamRun, name: str, payload: dict[str, Any]) -> None:
    root = Path(run.work_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


class ContractCoordinator(ScriptedCoordinator):
    """Adapts AssessorPort verdicts into RoundAssessment (no scientific ifs)."""

    def __init__(self, assessor: AssessorPort | None = None) -> None:
        self._assessor: AssessorPort = assessor or ScriptedAssessor()

    async def assess_round(self, run: TeamRun) -> RoundAssessment:
        waiting = await ScriptedCoordinator.assess_round(self, run)
        if waiting.decision != AssessmentDecision.COMPLETED:
            return waiting

        facts = build_assessment_facts(run)
        run.metadata["assessment_facts"] = facts
        wave = int(run.active_round or 0)
        _persist_json(run, f"assessment_facts_r{wave}.json", facts)

        spec = CoordinationSpec.from_run_metadata(dict(run.metadata or {}))
        if spec.stopping.kind == StoppingKind.VERIFICATION:
            # Verification-kind stopping: the exit artifact is verifier-owned,
            # so the verdict is a pure observation of the frozen verifier's
            # output. No LLM assess seat is consulted (by declaration).
            raw = facts_only_verdict(facts)
        else:
            raw = await self._assessor.assess(facts, run)
        if not isinstance(raw, dict):
            raw = {}
        assess_roles = [str(x) for x in (facts.get("assess_roles") or [])]
        assessor_key = str(raw.get("assessor_key") or "")
        if assessor_key and assess_roles:
            try:
                assert_assessor_authorized(assessor_key, assess_roles)
            except PermissionError:
                # Recoverable: ask the authorized assess seat to re-issue.
                verdict = RoundAssessment(
                    decision=AssessmentDecision.CONTINUE,
                    summary=f"unauthorized assessor {assessor_key}",
                    reason_code="illegal_assessment",
                    next_round_hint=(
                        f"assessor_key must be one of {sorted(assess_roles)}; "
                        "re-emit round_assessment JSON from an authorized seat"
                    ),
                )
                _persist_json(
                    run,
                    f"round_assessment_r{wave}.json",
                    {
                        "decision": verdict.decision.value,
                        "reason_code": verdict.reason_code,
                        "summary": verdict.summary,
                        "next_round_hint": verdict.next_round_hint,
                    },
                )
                return verdict

        if envelope_approves_promote(raw):
            artifact_path = str((run.metadata or {}).get("artifact_path") or "")
            if artifact_path and not Path(artifact_path).is_file():
                promoted = _try_promote_draft(dict(run.metadata or {}), artifact_path)
                if promoted is None and not Path(artifact_path).is_file():
                    verdict = RoundAssessment(
                        decision=AssessmentDecision.CONTINUE,
                        summary="assessor approved promote but no valid draft",
                        reason_code="promote_failed",
                        next_round_hint=(
                            "emit-duty seat must write a draft under draft_globs, "
                            "or write the canonical exit artifact before promote"
                        ),
                    )
                    _persist_json(
                        run,
                        f"round_assessment_r{wave}.json",
                        {
                            "decision": verdict.decision.value,
                            "reason_code": verdict.reason_code,
                            "summary": verdict.summary,
                            "next_round_hint": verdict.next_round_hint,
                        },
                    )
                    return verdict
                # Refresh facts after promote for audit trail
                facts = build_assessment_facts(run)
                run.metadata["assessment_facts"] = facts

        verdict = parse_assessor_envelope(raw)
        _persist_json(
            run,
            f"round_assessment_r{wave}.json",
            {
                "decision": verdict.decision.value,
                "reason_code": verdict.reason_code,
                "summary": verdict.summary,
                "next_round_hint": verdict.next_round_hint,
                "assessor_key": assessor_key,
                "raw": raw,
            },
        )
        return verdict
