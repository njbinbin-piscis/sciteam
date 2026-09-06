"""AssessorPort: institutional round verdicts (agent seat), not engine ifs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sciteam.models import AssessmentDecision, RoundAssessment, TeamRun

_REASON_CODES = {
    "ok_complete",
    "need_artifact",
    "gate_unsatisfied",
    "exit_stalled",
    "integrity_fail",
    "process_fail",
    "all_workers_failed",
    "illegal_assessment",
    "promote_failed",
}


@runtime_checkable
class AssessorPort(Protocol):
    async def assess(self, facts: dict[str, Any], run: TeamRun) -> dict[str, Any]: ...


def assert_assessor_authorized(agent_key: str, assess_roles: list[str]) -> None:
    key = str(agent_key or "").strip()
    roles = {str(r).strip() for r in assess_roles if str(r).strip()}
    if not key or (roles and key not in roles):
        raise PermissionError(f"assessor seat {key!r} not in assess_roles {sorted(roles)}")


_ILLEGAL_HINT = (
    "Re-emit round_assessment JSON only: decision "
    "(continue|completed|failed), reason_code (whitelist), summary, "
    "next_round_hint, assessor_key. No markdown, no prose-only reply."
)


def parse_assessor_envelope(raw: dict[str, Any] | None) -> RoundAssessment:
    """Validate assessor envelope.

    Illegal shape is a *recoverable mechanical fault*: CONTINUE so the seat can
    re-emit (or escalate to a human gate after a streak). Do not hard-stop the
    mission — fail-honest FAILED is reserved for institutional verdicts
    (exit_stalled, all_workers_failed, …).
    """
    if not isinstance(raw, dict):
        return RoundAssessment(
            decision=AssessmentDecision.CONTINUE,
            summary="illegal assessment: not an object",
            reason_code="illegal_assessment",
            next_round_hint=_ILLEGAL_HINT,
        )
    decision_raw = str(raw.get("decision") or "").strip().lower()
    reason = str(raw.get("reason_code") or "").strip()
    summary = str(raw.get("summary") or "")
    if decision_raw not in {"continue", "completed", "failed"}:
        return RoundAssessment(
            decision=AssessmentDecision.CONTINUE,
            summary="illegal assessment: bad decision",
            reason_code="illegal_assessment",
            next_round_hint=_ILLEGAL_HINT,
        )
    if reason not in _REASON_CODES:
        return RoundAssessment(
            decision=AssessmentDecision.CONTINUE,
            summary=f"illegal assessment: bad reason_code {reason!r}",
            reason_code="illegal_assessment",
            next_round_hint=_ILLEGAL_HINT,
        )
    try:
        decision = AssessmentDecision(decision_raw)
    except ValueError:
        return RoundAssessment(
            decision=AssessmentDecision.CONTINUE,
            summary="illegal assessment: decision enum",
            reason_code="illegal_assessment",
            next_round_hint=_ILLEGAL_HINT,
        )
    preferred = raw.get("preferred_agent")
    return RoundAssessment(
        decision=decision,
        summary=summary or reason,
        next_round_hint=str(raw.get("next_round_hint") or ""),
        preferred_agent=(str(preferred) if preferred else None),
        reason_code=reason,
        artifact_plan=(
            [{"kind": "team_summary", "content": summary}]
            if decision == AssessmentDecision.COMPLETED
            else []
        ),
    )


def envelope_approves_promote(raw: dict[str, Any] | None) -> bool:
    return bool(isinstance(raw, dict) and raw.get("approve_promote_draft") is True)


def facts_only_verdict(
    facts: dict[str, Any],
    *,
    assessor_key: str = "",
    promote_if_draft: bool = False,
) -> dict[str, Any]:
    """Mechanical verdict computed from observation facts alone (no LLM).

    Used by two institutional paths:
    - ScriptedAssessor (CI/test double) applies it under a protocol fixture;
    - verification-kind stopping, where the exit artifact is verifier-owned
      (only the frozen verifier may write it), so artifact existence/gate IS
      the verdict and no LLM assess seat may be consulted.
    """
    wave = list(facts.get("wave_tasks") or [])
    if wave:
        states = {str(t.get("state") or "") for t in wave}
        if states and states <= {"failed"} and "done" not in states:
            return {
                "decision": "failed",
                "reason_code": "all_workers_failed",
                "summary": "all production tasks failed",
                "next_round_hint": "all_workers_failed",
                "assessor_key": assessor_key,
            }

    exists = bool(facts.get("artifact_exists"))
    stall_n = facts.get("charter_stagnation_max_rounds")
    active = int(facts.get("active_round") or 0)
    emit = list(facts.get("artifact_emit_roles") or [])
    emit_hint = (
        f"seat(s) {', '.join(f'`{x}`' for x in emit)} must emit exit artifact"
        if emit
        else "emit-duty seat must write exit artifact"
    )

    if not exists:
        if stall_n is not None and int(stall_n) > 0 and active >= int(stall_n):
            return {
                "decision": "failed",
                "reason_code": "exit_stalled",
                "summary": f"no exit artifact after {active} rounds",
                "next_round_hint": f"exit_stalled: {emit_hint}",
                "assessor_key": assessor_key,
            }
        drafts = list(facts.get("draft_paths_found") or [])
        if drafts and promote_if_draft:
            return {
                "decision": "completed",
                "reason_code": "ok_complete",
                "summary": "approve promote of existing draft",
                "next_round_hint": "",
                "approve_promote_draft": True,
                "assessor_key": assessor_key,
            }
        return {
            "decision": "continue",
            "reason_code": "need_artifact",
            "summary": "exit artifact missing",
            "next_round_hint": emit_hint,
            "assessor_key": assessor_key,
        }

    gate = facts.get("artifact_gate")
    if isinstance(gate, dict) and gate.get("pointer"):
        observed = facts.get("gate_observed_value")
        expected = gate.get("equals")
        if observed != expected:
            return {
                "decision": "continue",
                "reason_code": "gate_unsatisfied",
                "summary": "artifact present but gate not satisfied",
                "next_round_hint": (
                    f"gate_unsatisfied: {json.dumps(gate)} (observed: {json.dumps(observed)})"
                ),
                "assessor_key": assessor_key,
            }

    return {
        "decision": "completed",
        "reason_code": "ok_complete",
        "summary": "exit artifact acceptable under protocol",
        "next_round_hint": "",
        "assessor_key": assessor_key,
    }


class ScriptedAssessor:
    """CI/test double: applies an external protocol fixture to facts only."""

    def __init__(self, protocol: dict[str, Any] | None = None) -> None:
        self._protocol = dict(protocol or {})

    @classmethod
    def from_yaml(cls, path: Path | str) -> ScriptedAssessor:
        import yaml

        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: assessor protocol must be a mapping")
        return cls(raw)

    async def assess(self, facts: dict[str, Any], run: TeamRun) -> dict[str, Any]:
        del run
        assess_roles = [str(x) for x in (facts.get("assess_roles") or [])]
        assessor_key = str(
            self._protocol.get("assessor_key")
            or (assess_roles[0] if assess_roles else "round_assessor")
        )
        return facts_only_verdict(
            facts,
            assessor_key=assessor_key,
            promote_if_draft=bool(self._protocol.get("promote_if_draft")),
        )


_LAB_ROOT = Path(__file__).resolve().parents[1]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _load_role_prompt(role: str, *, assets_root: Path) -> str:
    """Load L2 role prompt for the assess seat (no hardcoded paradigm names)."""
    role = str(role or "").strip() or "round_assessor"
    prompts = assets_root / "prompts" / "roles"
    for name in (f"{role}.md", "round_assessor.md"):
        text = _read_text(prompts / name)
        if text.strip():
            return text
    return (
        f"You are the `{role}` institutional assessor. "
        "Apply charter text to the observation pack and return round_assessment JSON."
    )


def _load_assess_skill(*, assets_root: Path) -> str:
    return _read_text(assets_root / "skills" / "institution.round_assess" / "SKILL.md")


class LlmAssessorPort:
    """Production assessor: one seat inference using the shared LLM worker stack."""

    def __init__(
        self,
        *,
        runtime: Any,
        assets_root: Path | str | None = None,
        role_prompt: str = "",
        skill_text: str = "",
    ) -> None:
        self._runtime = runtime
        self._assets_root = Path(assets_root) if assets_root else _LAB_ROOT / "assets"
        self._role_prompt_override = role_prompt
        self._skill_text_override = skill_text

    def _resolve_assets_root(self, run: TeamRun) -> Path:
        meta = run.metadata or {}
        raw = meta.get("assets_root")
        if raw:
            return Path(str(raw))
        return self._assets_root

    async def assess(self, facts: dict[str, Any], run: TeamRun) -> dict[str, Any]:
        assess_roles = [str(x) for x in (facts.get("assess_roles") or [])]
        seat = None
        for agent in run.agents:
            role = str(agent.profile.get("role") or agent.agent_key)
            if agent.agent_key in assess_roles or role in assess_roles:
                seat = agent
                break
        if seat is None:
            return {
                "decision": "failed",
                "reason_code": "illegal_assessment",
                "summary": "no assess seat in run",
                "assessor_key": "",
            }
        assets = self._resolve_assets_root(run)
        role = str(seat.profile.get("role") or seat.agent_key)
        role_prompt = self._role_prompt_override or _load_role_prompt(role, assets_root=assets)
        skill_text = self._skill_text_override or _load_assess_skill(assets_root=assets)
        prompt = (
            "# Round assessment duty\n"
            "Apply the charter/protocol to the observation pack. "
            "Return a JSON object matching round_assessment schema "
            "(decision, reason_code, summary, next_round_hint).\n"
            "Do NOT write the mission exit-contract file.\n\n"
            f"# Seat\n{seat.agent_key} (role={role})\n\n"
            f"# Role guidance\n{role_prompt}\n\n"
            f"# Skill\n{skill_text}\n\n"
            f"# Observation pack (facts only)\n```json\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=2)}\n```\n"
        )
        # Prefer a dedicated assess skills allowlist so the worker loads the skill pack.
        meta = dict(run.metadata or {})
        meta["assessment_mode"] = True
        meta["skills_allowlist"] = list(
            dict.fromkeys(["institution.round_assess", *list(meta.get("skills_allowlist") or [])])
        )
        result = await self._runtime.run_subagent(
            agent_id=str(seat.profile.get("agent_id") or seat.agent_key),
            task=prompt,
            work_dir=seat.work_dir,
            parent_run_id=run.id,
            context={
                "team_agent_key": seat.agent_key,
                "team_run_id": run.id,
                "team_metadata": meta,
            },
        )
        output = str(getattr(result, "output", "") or "")
        parsed = _extract_json_object(output)
        if not isinstance(parsed, dict):
            # Worker may wrap JSON in summary — try envelope.artifact / common keys.
            try:
                from sciteam.llm_worker import _extract_json as _worker_json

                wrapped = _worker_json(output)
                if isinstance(wrapped, dict):
                    if isinstance(wrapped.get("artifact"), dict):
                        parsed = wrapped["artifact"]
                    elif "decision" in wrapped:
                        parsed = wrapped
            except Exception:  # noqa: BLE001
                parsed = None
        if not isinstance(parsed, dict):
            return {
                "decision": "failed",
                "reason_code": "illegal_assessment",
                "summary": "assessor returned non-JSON envelope",
                "assessor_key": seat.agent_key,
            }
        parsed.setdefault("assessor_key", seat.agent_key)
        return parsed


def make_contract_coordinator(
    *,
    live: bool = False,
    runtime: Any = None,
    assets_root: Path | str | None = None,
    assessor: AssessorPort | None = None,
) -> Any:
    """Composition-root helper: live → LlmAssessorPort; else ScriptedAssessor.

    Tests may pass ``assessor=`` explicitly. Harness live paths must set live=True.
    """
    from sciteam.coordinator import ContractCoordinator

    if assessor is not None:
        return ContractCoordinator(assessor)
    if live:
        if runtime is None:
            raise ValueError("make_contract_coordinator(live=True) requires runtime")
        root = Path(assets_root) if assets_root else _LAB_ROOT / "assets"
        return ContractCoordinator(LlmAssessorPort(runtime=runtime, assets_root=root))
    return ContractCoordinator(ScriptedAssessor())


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
