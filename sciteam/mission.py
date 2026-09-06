"""MissionSpec + exit-contract validation around the team orchestrator.

A mission is one team run with a goal, a paradigm (coordination template id)
and an exit contract (JSON Schema). The engine runs every mission the same
way; mission *kinds* are catalog labels carried as data, never branched on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sciteam.budget_gate import (
    classify_hold_reason,
    clear_gate,
    gate_enabled,
    grant_forbidden,
    open_gate,
    read_gate,
    write_schedule_meter,
)
from sciteam.capabilities import PreflightResult, RuntimeCapabilities, preflight_mission
from sciteam.coordination import CoordinationSpec
from sciteam.experiment_pack import PackError
from sciteam.hr_runtime import StaffingResult, run_hr_staffing_session
from sciteam.models import TeamRunState
from sciteam.orchestrator import CreateSpec, TeamOrchestrator
from sciteam.roster_policy import (
    load_role_catalog,
    prefer_skills_for_roles,
    role_registry_preflight,
    roster_policy_enabled,
    validate_roster,
)
from sciteam.team_loader import TeamRegistry, resolve_start_params

try:
    import jsonschema
except ImportError:  # pragma: no cover - dependency declared in pyproject
    jsonschema = None


class MissionSpecError(ValueError):
    pass


@dataclass(frozen=True)
class MissionBudget:
    max_rounds: int = 0
    max_wall_clock_seconds: int = 0
    max_llm_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MissionBudget:
        data = data or {}
        return cls(
            max_rounds=int(data.get("max_rounds") or 0),
            max_wall_clock_seconds=int(data.get("max_wall_clock_seconds") or 0),
            max_llm_tokens=int(data.get("max_llm_tokens") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_llm_tokens": self.max_llm_tokens,
        }


@dataclass(frozen=True)
class MissionSpec:
    id: str
    goal: str
    paradigm: str
    exit_contract: str
    kind: str = ""
    roster: list[dict[str, Any]] | None = None
    skills_allowlist: tuple[str, ...] = ()
    budget: MissionBudget = field(default_factory=MissionBudget)
    inputs: dict[str, Any] = field(default_factory=dict)
    coordination_overrides: dict[str, Any] = field(default_factory=dict)
    on_fail: str = "return_to_campaign_planner"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionSpec:
        if not isinstance(data, dict):
            raise MissionSpecError("mission spec must be a mapping")
        for key in ("id", "goal", "paradigm", "exit_contract"):
            if not str(data.get(key) or "").strip():
                raise MissionSpecError(f"mission spec missing required field: {key}")
        roster = data.get("roster")
        if roster is not None:
            if not isinstance(roster, list) or not all(isinstance(x, dict) for x in roster):
                raise MissionSpecError("mission roster must be a list of mappings")
            for item in roster:
                if not str(item.get("agent_key") or "").strip():
                    raise MissionSpecError("mission roster entries need agent_key")
        skills = data.get("skills_allowlist") or []
        if not isinstance(skills, (list, tuple)):
            raise MissionSpecError("skills_allowlist must be a list")
        return cls(
            id=str(data["id"]),
            goal=str(data["goal"]),
            paradigm=str(data["paradigm"]),
            exit_contract=str(data["exit_contract"]),
            kind=str(data.get("kind") or ""),
            roster=roster,
            skills_allowlist=tuple(str(s) for s in skills),
            budget=MissionBudget.from_dict(data.get("budget")),
            inputs=dict(data.get("inputs") or {}),
            coordination_overrides=dict(data.get("coordination_overrides") or {}),
            on_fail=str(data.get("on_fail") or "return_to_campaign_planner"),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "goal": self.goal,
            "paradigm": self.paradigm,
            "exit_contract": self.exit_contract,
            "kind": self.kind,
            "skills_allowlist": list(self.skills_allowlist),
            "budget": self.budget.to_dict(),
            "inputs": dict(self.inputs),
            "coordination_overrides": dict(self.coordination_overrides),
            "on_fail": self.on_fail,
            "metadata": dict(self.metadata),
        }
        if self.roster is not None:
            out["roster"] = [dict(x) for x in self.roster]
        return out


def _record_effective_skills(
    session_dir: str,
    *,
    mission_id: str,
    mission_declared: list[str],
    role_derived: list[str],
    effective: list[str],
) -> None:
    """A4: write the union's provenance next to ``staffing_report.json``.

    Before A4 the mission-declared allowlist silently replaced role/HR
    training whenever it was non-empty; this file is the audit trail that
    the union actually ran and what each side contributed, so a reviewer can
    see HR's differentiated skills reached the worker rather than being
    dropped a second time by some other silent override.
    """
    try:
        path = Path(session_dir) / "effective_skills.json"
        path.write_text(
            json.dumps(
                {
                    "mission_id": mission_id,
                    "mission_declared": mission_declared,
                    "role_derived": role_derived,
                    "effective_union": effective,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


class ContractValidator:
    """Validates mission artifacts against JSON Schemas in a schemas dir."""

    def __init__(self, schemas_dir: Path | str) -> None:
        self._dir = Path(schemas_dir)

    @property
    def schemas_dir(self) -> Path:
        return self._dir

    def schema_path(self, contract: str) -> Path:
        candidate = Path(contract)
        if candidate.suffix == ".json" and candidate.is_absolute():
            return candidate
        name = contract
        if not name.endswith(".schema.json"):
            name = f"{name}.schema.json"
        return self._dir / name

    def artifact_name(self, contract: str) -> str:
        stem = Path(contract).name
        for suffix in (".schema.json", ".json"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        return f"{stem}.json"

    def validate(self, contract: str, artifact: dict[str, Any]) -> list[str]:
        schema_file = self.schema_path(contract)
        if not schema_file.is_file():
            return [f"contract schema not found: {schema_file}"]
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        if jsonschema is None:
            return ["jsonschema package unavailable"]
        validator = jsonschema.Draft202012Validator(schema)
        return [
            f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in sorted(validator.iter_errors(artifact), key=lambda e: list(e.absolute_path))
        ]

    def validate_file(self, contract: str, artifact_path: Path) -> list[str]:
        if not artifact_path.is_file():
            return [f"artifact not found: {artifact_path}"]
        try:
            data = json.loads(artifact_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return [f"artifact is not valid JSON: {exc}"]
        if not isinstance(data, dict):
            return ["artifact root must be a JSON object"]
        return self.validate(contract, data)


@dataclass(frozen=True)
class MissionOutcome:
    mission_id: str
    status: str  # completed | contract_failed | failed | budget_exhausted | waiting_user
    team_run_id: str
    rounds_used: int
    wall_clock_seconds: float
    contract_ok: bool
    contract_errors: list[str] = field(default_factory=list)
    artifact_path: str = ""
    paradigm: str = ""
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.contract_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "status": self.status,
            "team_run_id": self.team_run_id,
            "rounds_used": self.rounds_used,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "contract_ok": self.contract_ok,
            "contract_errors": list(self.contract_errors),
            "artifact_path": self.artifact_path,
            "paradigm": self.paradigm,
            "detail": self.detail,
        }


def _tighten_coordination(
    base: dict[str, Any] | None,
    overrides: dict[str, Any],
    budget: MissionBudget,
) -> dict[str, Any]:
    """Merge overrides onto the paradigm coordination; budgets only tighten.

    ``SCITEAM_MAX_ITERATIONS`` (operator / control-console) sets the paradigm
    round ceiling for this process when present.
    """
    import os

    merged: dict[str, Any] = json.loads(json.dumps(base or {}))
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    env_iter = int(os.environ.get("SCITEAM_MAX_ITERATIONS") or 0)
    if env_iter > 0:
        stopping = merged.setdefault("stopping", {})
        stopping["max_iterations"] = env_iter
    if budget.max_rounds > 0:
        stopping = merged.setdefault("stopping", {})
        current = int(stopping.get("max_total_rounds") or 0)
        stopping["max_total_rounds"] = (
            budget.max_rounds if current <= 0 else min(current, budget.max_rounds)
        )
    return merged


class MissionRunner:
    """Drives one mission on the generic team orchestrator + validates its exit contract."""

    def __init__(
        self,
        *,
        orchestrator: TeamOrchestrator,
        paradigms: TeamRegistry,
        validator: ContractValidator,
        artifacts_root: Path | str,
        skills_dir: Path | str | None = None,
        capabilities: RuntimeCapabilities | None = None,
        pack_dir: Path | str | None = None,
        hr_client: Any | None = None,
        audit_runner: Any | None = None,
        prompts_dir: Path | str | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._paradigms = paradigms
        self._validator = validator
        self._artifacts_root = Path(artifacts_root)
        self._skills_dir = Path(skills_dir) if skills_dir is not None else None
        self._capabilities = capabilities
        self._pack_dir = Path(pack_dir) if pack_dir is not None else None
        self._hr_client = hr_client
        # A3: when set, every resolved seat must have both a CATALOG.yaml
        # entry and a dedicated prompts/roles/<role>.md — fail closed rather
        # than silently falling back to worker.md + skills=[].
        self._prompts_dir = Path(prompts_dir) if prompts_dir is not None else None
        # Injected declared-audit executor: (tool_name, arguments) -> result
        # dict. Defaults to the research tool registry. What any declared audit
        # checks is the template's business; this class only runs it.
        self._audit_runner = audit_runner

    def artifact_path(self, spec: MissionSpec) -> Path:
        return self._artifacts_root / spec.id / self._validator.artifact_name(spec.exit_contract)

    def _declared_audit_errors(self, spec: MissionSpec, artifact_file: Path) -> list[str]:
        """Run the audit the mission declares over its exit artifact, if any.

        Semantics-free mechanism: ``metadata.artifact_audit = {"tool": name,
        "pointer": json-pointer}`` names a registered tool and the artifact
        slice it judges. The engine resolves the pointer, hands the slice to
        the tool, and requires an ok/passed verdict — it does not know what
        the tool checks. The norm lives in the mission spec (data); this is
        the same shape as ``artifact_gate``, generalized from an equality test
        to a declared verifier.
        """
        decl = (spec.metadata or {}).get("artifact_audit")
        if not isinstance(decl, dict) or not decl.get("tool"):
            return []
        try:
            artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"artifact_audit: cannot read artifact: {exc}"]
        from sciteam.assessment_facts import _resolve_pointer

        payload = _resolve_pointer(artifact, str(decl.get("pointer") or ""))
        runner = self._audit_runner
        if runner is None:
            from sciteam.wizard_tools import run_research_tool

            runner = run_research_tool
        tool_name = str(decl["tool"])
        try:
            result = runner(tool_name, {"payload": payload})
        except Exception as exc:  # noqa: BLE001 — a crashed auditor must fail closed
            return [f"artifact_audit[{tool_name}] crashed: {exc}"]
        if isinstance(result, dict) and (result.get("passed") is True or result.get("ok") is True):
            return []
        detail = (
            json.dumps(result.get("failures") or result, ensure_ascii=False)[:600]
            if isinstance(result, dict)
            else str(result)[:600]
        )
        return [f"artifact_audit[{tool_name}] failed: {detail}"]

    def _resolve_pack_dir(self, spec: MissionSpec) -> Path | None:
        candidates = [
            spec.metadata.get("pack_dir"),
            os.environ.get("SCITEAM_PACK_DIR"),
            str(self._pack_dir) if self._pack_dir else None,
        ]
        for raw in candidates:
            if not raw:
                continue
            path = Path(str(raw))
            # Accept either .../pack or the run_dir that contains pack/
            if (path / "agents").is_dir() or (path / "AGENT.md").is_file():
                return path if path.name == "pack" or (path / "agents").is_dir() else path
            if (path / "pack" / "agents").is_dir() or (path / "pack").exists():
                return path / "pack"
            if path.name == "pack":
                return path
        return None

    def capability_preflight(
        self,
        allowlist: list[str] | tuple[str, ...],
    ) -> PreflightResult | None:
        """Return preflight result when skills_dir + capabilities are configured."""
        if self._skills_dir is None or self._capabilities is None:
            return None
        return preflight_mission(
            skills_dir=self._skills_dir,
            allowlist=allowlist,
            available=self._capabilities,
        )

    async def run(self, spec: MissionSpec, *, max_ticks: int | None = None) -> MissionOutcome:
        template = self._paradigms.get(spec.paradigm)
        if template is None:
            raise MissionSpecError(f"unknown paradigm template: {spec.paradigm}")
        agents, coordination = resolve_start_params(spec.paradigm, registry=self._paradigms)
        if spec.roster is not None:
            agents = [dict(x) for x in spec.roster]
        if roster_policy_enabled():
            violations = validate_roster(agents, catalog=load_role_catalog())
            if violations:
                detail = "; ".join(v.format() for v in violations)
                return MissionOutcome(
                    mission_id=spec.id,
                    status="failed",
                    team_run_id="",
                    rounds_used=0,
                    wall_clock_seconds=0.0,
                    contract_ok=False,
                    contract_errors=[v.format() for v in violations],
                    artifact_path="",
                    paradigm=spec.paradigm,
                    detail=f"roster policy violated: {detail}",
                )

        # G6: dedicated hr_officer LLM session (observable under pack/hr_sessions/).
        hr_detail = ""
        pack = self._resolve_pack_dir(spec)
        staffing: StaffingResult | None = None
        if pack is not None:
            try:
                run_dir = pack.parent if pack.name == "pack" else pack
                staffing = await run_hr_staffing_session(
                    run_dir,
                    agents,
                    mission_id=spec.id,
                    mission_goal=spec.goal,
                    client=self._hr_client,
                )
                agents = staffing.roster
                hr_detail = (
                    f"HR[{staffing.mode}] session={Path(staffing.session_dir).name}"
                    f" tools={staffing.to_dict().get('n_tool_calls', 0)}"
                )
                if self._skills_dir is None:
                    self._skills_dir = Path(run_dir) / "pack" / "skills"
            except PackError as exc:
                return MissionOutcome(
                    mission_id=spec.id,
                    status="failed",
                    team_run_id="",
                    rounds_used=0,
                    wall_clock_seconds=0.0,
                    contract_ok=False,
                    contract_errors=[str(exc)],
                    artifact_path="",
                    paradigm=spec.paradigm,
                    detail=f"HR staffing failed: {exc}",
                )

        if self._prompts_dir is not None:
            role_issues = role_registry_preflight(agents, prompts_dir=self._prompts_dir)
            if role_issues:
                detail = "; ".join(v.format() for v in role_issues)
                detail_parts = [
                    p for p in (hr_detail, f"role registry preflight failed: {detail}") if p
                ]
                return MissionOutcome(
                    mission_id=spec.id,
                    status="failed",
                    team_run_id="",
                    rounds_used=0,
                    wall_clock_seconds=0.0,
                    contract_ok=False,
                    contract_errors=[v.format() for v in role_issues],
                    artifact_path="",
                    paradigm=spec.paradigm,
                    detail="; ".join(detail_parts),
                )

        # A4: the mission-declared allowlist is a floor, not a ceiling. It used
        # to *replace* role/HR-derived skills whenever it was non-empty, which
        # silently discarded per-role training (e.g. a `reviewer` seat's HR-
        # recruited `lit.citation_guard`) any time a paradigm runner set a
        # uniform per-contract list — the exact gap A3's role registry fix
        # would otherwise be defeated by. Union instead, in declared order.
        skills_allowlist = list(spec.skills_allowlist)
        role_ids = [str(a.get("role") or a.get("agent_key") or "") for a in agents]
        from_pack: list[str] = []
        for a in agents:
            for sid in a.get("skills") or []:
                if sid not in from_pack:
                    from_pack.append(str(sid))
        role_derived = from_pack or prefer_skills_for_roles(role_ids)
        mission_declared = list(spec.skills_allowlist)
        for sid in role_derived:
            if sid not in skills_allowlist:
                skills_allowlist.append(sid)

        if pack is not None and staffing is not None:
            _record_effective_skills(
                staffing.session_dir,
                mission_id=spec.id,
                mission_declared=mission_declared,
                role_derived=role_derived,
                effective=skills_allowlist,
            )

        # Professional preflight: skill-declared capabilities must be available.
        pf = self.capability_preflight(skills_allowlist)
        if pf is not None and not pf.ok:
            return MissionOutcome(
                mission_id=spec.id,
                status="failed",
                team_run_id="",
                rounds_used=0,
                wall_clock_seconds=0.0,
                contract_ok=False,
                contract_errors=[i.format() for i in pf.issues],
                artifact_path="",
                paradigm=spec.paradigm,
                detail=pf.summary(),
            )

        coordination = _tighten_coordination(coordination, spec.coordination_overrides, spec.budget)
        coord_spec = CoordinationSpec.from_dict(coordination)

        artifact_file = self.artifact_path(spec)
        artifact_file.parent.mkdir(parents=True, exist_ok=True)

        # Tick budget must cover many CONTINUE rounds under an artifact_gate.
        # Each round needs plan + per-agent dispatches + assess; 64 was a silent
        # killer that aborted still-running teams as "failed".
        round_cap = (
            coord_spec.stopping.max_total_rounds or coord_spec.stopping.max_iterations or 200
        )
        if spec.budget.max_rounds > 0:
            round_cap = (
                min(round_cap, spec.budget.max_rounds) if round_cap else spec.budget.max_rounds
            )

        def _with_hr(detail: str) -> str:
            parts = [p for p in (hr_detail, detail) if p]
            return "; ".join(parts)

        n_agents = max(1, len(agents))
        # Tick is an internal scheduler step counter — NOT a scientific stop condition.
        # Explicit max_ticks (tests) stays hard; auto-derived budgets are soft and refill
        # so paper runs are not interrupted by an opaque harness ceiling.
        tick_hard = max_ticks is not None or (
            (os.environ.get("SCITEAM_TICK_HARD_GATE") or "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        tick_budget = (
            int(max_ticks)
            if max_ticks is not None
            else max(2000, int(round_cap) * (n_agents + 4) * 2)
        )
        tick_refills = 0

        emit_roles = list(template.artifact_emit_roles())
        assess_roles = list(template.assess_roles())
        meta_in = dict(spec.metadata or {})
        # Stagnation threshold is L2 (charter / explicit metadata) — never invented here.
        charter = (coordination or {}).get("charter") or {}
        if (
            "stagnation_max_rounds" not in meta_in
            and isinstance(charter, dict)
            and charter.get("stagnation_max_rounds")
        ):
            meta_in["stagnation_max_rounds"] = int(charter["stagnation_max_rounds"])
        draft_globs = meta_in.get("draft_globs")
        if not isinstance(draft_globs, (list, tuple)):
            draft_globs = ["*_draft.json", "*_draft*.json"]
        assets_root = meta_in.get("assets_root")
        if not assets_root and self._skills_dir is not None:
            # skills_dir is …/assets/skills → assets root for assessor L2 loads
            assets_root = str(Path(self._skills_dir).parent)
        # 2026-08-15 audit fix (E4(ii) follow-up): assess-duty seats audit
        # artifacts that legitimately reference paths outside their own
        # mission subdirectory (e.g. package_manifest.json's contents[]
        # point at campaign-root files like protocol.yaml). Production
        # seats stay confined to their own mission workspace (unchanged);
        # only assessment_mode calls consult campaign_run_dir to widen
        # their read-only audit scope to the whole campaign — identically
        # for both RuntimePort implementations (see llm_worker.py /
        # pi_runtime.py `assessment_mode` branches). None when the runner
        # cannot resolve a campaign root (e.g. some harness/test setups).
        campaign_dir = self._campaign_run_dir(spec)
        run = self._orchestrator.create_run(
            CreateSpec(
                team_profile_id=spec.paradigm,
                goal=spec.goal,
                agents=agents,
                coordination=coordination,
                metadata={
                    **meta_in,
                    "mission_id": spec.id,
                    "mission_kind": spec.kind,
                    "mission_inputs": dict(spec.inputs),
                    "skills_allowlist": list(skills_allowlist),
                    "exit_contract": spec.exit_contract,
                    "artifact_path": str(artifact_file),
                    # Paradigm seats allowed to write exit artifact (worker duty).
                    "artifact_emit_roles": emit_roles,
                    "assess_roles": assess_roles,
                    "draft_globs": list(draft_globs),
                    "schemas_dir": str(self._validator.schemas_dir),
                    **({"assets_root": str(assets_root)} if assets_root else {}),
                    **({"pack_dir": str(pack)} if pack is not None else {}),
                    **({"hr_staffing": hr_detail} if hr_detail else {}),
                    **({"campaign_run_dir": str(campaign_dir)} if campaign_dir is not None else {}),
                },
            )
        )

        started = time.monotonic()
        state = TeamRunState.PENDING
        ticks_used = 0
        ticks_remaining = max(1, tick_budget)
        wall_limit = int(spec.budget.max_wall_clock_seconds or 0)
        abort_detail = ""
        # After grant_unlimited, do not re-stop on the harness tick ceiling.
        tick_unlimited = False

        while ticks_remaining > 0 or tick_unlimited:
            result = await self._orchestrator.tick(run.id)
            ticks_used += 1
            if not tick_unlimited:
                ticks_remaining -= 1
            state = result.state
            elapsed = time.monotonic() - started
            if campaign_dir and (ticks_used == 1 or ticks_used % 5 == 0 or ticks_remaining <= 0):
                with contextlib.suppress(OSError):
                    write_schedule_meter(
                        campaign_dir,
                        mission_id=spec.id,
                        team_run_id=run.id,
                        ticks_used=ticks_used,
                        tick_budget=tick_budget,
                        ticks_remaining=0 if tick_unlimited else ticks_remaining,
                        rounds_used=int(self._orchestrator.require(run.id).active_round),
                        round_cap=int(round_cap),
                        n_agents=n_agents,
                        state=str(state.value if hasattr(state, "value") else state),
                    )

            if state in {TeamRunState.COMPLETED, TeamRunState.FAILED, TeamRunState.CANCELLED}:
                break

            if state == TeamRunState.WAITING_USER:
                final_peek = self._orchestrator.require(run.id)
                gate_reason = str(final_peek.metadata.get("gate_reason") or "")
                if final_peek.metadata.get("budget_exhausted") or gate_reason == "budget_exhausted":
                    # Recover checkpoints written by the former human
                    # budget-gate path.  Initial round counts are never a
                    # reason to wait for an operator in a research campaign.
                    coord = CoordinationSpec.from_run_metadata(final_peek.metadata)
                    prior = (
                        coord.stopping.max_total_rounds
                        or coord.stopping.max_iterations
                        or int(final_peek.active_round)
                    )
                    self._orchestrator.extend_budget(
                        run.id,
                        extra_iterations=max(1, int(prior)),
                        note="automatic continuation after initial iteration scale",
                    )
                    if campaign_dir:
                        clear_gate(campaign_dir)
                    ticks_remaining = max(
                        ticks_remaining, self._ticks_for_grant({}, n_agents=n_agents)
                    )
                    continue
                # Budget exhaustion OR recoverable mechanical faults → human gate,
                # not silent campaign death.
                awaitable_gate = bool(
                    final_peek.metadata.get("budget_exhausted")
                    or gate_reason
                    in {
                        "assessor_envelope_repair",
                        "human_gate",
                        "budget_exhausted",
                        "worker_escalate",
                        "worker_retry_exhausted",
                        "billing_402",
                        "token_fuse",
                        "wall_fuse",
                    }
                )
                batch_worker_failure = spec.kind == "evo_task" and gate_reason in {
                    "worker_escalate",
                    "worker_retry_exhausted",
                }
                if awaitable_gate and gate_enabled() and campaign_dir and not batch_worker_failure:
                    raw_reason = (
                        "budget_exhausted"
                        if final_peek.metadata.get("budget_exhausted")
                        or gate_reason == "budget_exhausted"
                        else gate_reason or "human_gate"
                    )
                    detail = str(
                        final_peek.metadata.get("next_round_hint")
                        or final_peek.metadata.get("last_assessment_reason")
                        or gate_reason
                        or ""
                    )
                    reason = classify_hold_reason(reason=raw_reason, detail=detail)
                    granted = await self._await_budget_grant(
                        campaign_dir,
                        mission_id=spec.id,
                        team_run_id=run.id,
                        reason=reason,
                        detail=detail,
                    )
                    if granted is None:
                        abort_detail = "operator aborted at budget gate"
                        break
                    if granted.get("unlimited"):
                        tick_unlimited = True
                    extra_ticks = self._ticks_for_grant(granted, n_agents=n_agents)
                    ticks_remaining = max(ticks_remaining, extra_ticks)
                    self._orchestrator.extend_budget(
                        run.id,
                        extra_iterations=int(granted.get("extra_iterations") or 2),
                        unlimited=bool(granted.get("unlimited")),
                        note=f"human grant after {reason}",
                    )
                    continue
                break

            if 0 < wall_limit <= elapsed:
                if gate_enabled() and campaign_dir:
                    granted = await self._await_budget_grant(
                        campaign_dir,
                        mission_id=spec.id,
                        team_run_id=run.id,
                        reason="wall_clock_exhausted",
                        detail=f"mission wall clock budget exhausted ({wall_limit}s)",
                    )
                    if granted is None:
                        abort_detail = "operator aborted at budget gate (wall clock)"
                        break
                    if granted.get("unlimited"):
                        wall_limit = 0
                        tick_unlimited = True
                    else:
                        # Each granted iteration ≈ 3 minutes of wall clock headroom.
                        wall_limit = (
                            int(elapsed) + max(1, int(granted.get("extra_iterations") or 1)) * 180
                        )
                    extra_ticks = self._ticks_for_grant(granted, n_agents=n_agents)
                    ticks_remaining = max(ticks_remaining, extra_ticks)
                    continue
                break

            if (not tick_unlimited) and ticks_remaining <= 0 and state == TeamRunState.RUNNING:
                if tick_hard:
                    # Test / explicit hard gate only — never the paper default path.
                    if gate_enabled() and campaign_dir:
                        granted = await self._await_budget_grant(
                            campaign_dir,
                            mission_id=spec.id,
                            team_run_id=run.id,
                            reason="tick_budget_exhausted",
                            detail=(
                                f"mission tick budget exhausted ({ticks_used} ticks, "
                                f"round_cap={round_cap}; SCITEAM_TICK_HARD_GATE)"
                            ),
                            ticks_used=ticks_used,
                            tick_budget=tick_budget,
                        )
                        if granted is None:
                            abort_detail = "operator aborted at budget gate (ticks)"
                            break
                        if granted.get("unlimited"):
                            tick_unlimited = True
                        ticks_remaining = self._ticks_for_grant(granted, n_agents=n_agents)
                        continue
                    abort_detail = (
                        f"mission tick hard-stop ({ticks_used} ticks, round_cap={round_cap})"
                    )
                    break
                # Soft refill: more scheduler steps — never a scientific stop.
                # If coordinator round cap is already reached, do not spin forever.
                active_round = int(self._orchestrator.require(run.id).active_round)
                if round_cap > 0 and active_round >= int(round_cap):
                    abort_detail = (
                        f"scheduler idle at round_cap={round_cap} "
                        f"(ticks_used={ticks_used}); scientific budget owns the stop"
                    )
                    break
                chunk = max(500, (n_agents + 4) * 40)
                ticks_remaining = chunk
                tick_budget += chunk
                tick_refills += 1
                if campaign_dir:
                    with contextlib.suppress(OSError):
                        write_schedule_meter(
                            campaign_dir,
                            mission_id=spec.id,
                            team_run_id=run.id,
                            ticks_used=ticks_used,
                            tick_budget=tick_budget,
                            ticks_remaining=ticks_remaining,
                            rounds_used=int(self._orchestrator.require(run.id).active_round),
                            round_cap=int(round_cap),
                            n_agents=n_agents,
                            state=f"tick_soft_refill#{tick_refills}",
                        )
                continue

            await asyncio.sleep(0)

        final = self._orchestrator.require(run.id)
        wall = time.monotonic() - started
        rounds = final.active_round
        heartbeats = int((final.metadata or {}).get("heartbeats_used") or 0)
        # rounds_used = coordinator round index (budget semantics unchanged).
        # heartbeats_used lives in metadata / detail for employment observability.
        rounds_used = rounds
        hb_note = f"; heartbeats_used={heartbeats}" if heartbeats else ""
        if tick_refills:
            hb_note = f"; tick_soft_refills={tick_refills}{hb_note}"
        if abort_detail:
            hb_note = f"; {abort_detail}{hb_note}"

        if final.state == TeamRunState.COMPLETED:
            if campaign_dir is not None:
                clear_gate(campaign_dir)
            errors = self._validator.validate_file(spec.exit_contract, artifact_file)
            if not errors:
                errors = self._declared_audit_errors(spec, artifact_file)
            if errors:
                return MissionOutcome(
                    mission_id=spec.id,
                    status="contract_failed",
                    team_run_id=run.id,
                    rounds_used=rounds_used,
                    wall_clock_seconds=wall,
                    contract_ok=False,
                    contract_errors=errors,
                    artifact_path=str(artifact_file),
                    paradigm=spec.paradigm,
                    detail=_with_hr("team completed but exit contract failed validation" + hb_note),
                )
            return MissionOutcome(
                mission_id=spec.id,
                status="completed",
                team_run_id=run.id,
                rounds_used=rounds_used,
                wall_clock_seconds=wall,
                contract_ok=True,
                artifact_path=str(artifact_file),
                paradigm=spec.paradigm,
                detail=_with_hr(hb_note.lstrip("; ").strip()),
            )

        if final.state == TeamRunState.WAITING_USER:
            gate_reason = str(final.metadata.get("gate_reason") or "")
            status = (
                "budget_exhausted"
                if final.metadata.get("budget_exhausted")
                else (
                    "failed"
                    if spec.kind == "evo_task"
                    and gate_reason in {"worker_escalate", "worker_retry_exhausted"}
                    else "waiting_user"
                )
            )
            return MissionOutcome(
                mission_id=spec.id,
                status=status,
                team_run_id=run.id,
                rounds_used=rounds_used,
                wall_clock_seconds=wall,
                contract_ok=False,
                artifact_path=str(artifact_file),
                paradigm=spec.paradigm,
                detail=_with_hr(str(final.metadata.get("gate_reason") or abort_detail or "")),
            )

        if 0 < wall_limit <= wall and final.state == TeamRunState.RUNNING:
            return MissionOutcome(
                mission_id=spec.id,
                status="budget_exhausted",
                team_run_id=run.id,
                rounds_used=rounds_used,
                wall_clock_seconds=wall,
                contract_ok=False,
                artifact_path=str(artifact_file),
                paradigm=spec.paradigm,
                detail=_with_hr(abort_detail or "mission wall clock budget exhausted"),
            )

        # Still RUNNING only if an explicit hard tick stop (tests) aborted the loop.
        if final.state == TeamRunState.RUNNING:
            return MissionOutcome(
                mission_id=spec.id,
                status="budget_exhausted" if tick_hard else "failed",
                team_run_id=run.id,
                rounds_used=rounds_used,
                wall_clock_seconds=wall,
                contract_ok=False,
                artifact_path=str(artifact_file),
                paradigm=spec.paradigm,
                detail=_with_hr(
                    abort_detail
                    or (
                        f"mission stopped while team still running "
                        f"(ticks_used={ticks_used}, round_cap={round_cap}, hard={tick_hard})"
                    )
                ),
            )

        return MissionOutcome(
            mission_id=spec.id,
            status="failed",
            team_run_id=run.id,
            rounds_used=rounds_used,
            wall_clock_seconds=wall,
            contract_ok=False,
            artifact_path=str(artifact_file),
            paradigm=spec.paradigm,
            detail=_with_hr(abort_detail or f"team run ended in state {final.state.value}"),
        )

    def _campaign_run_dir(self, spec: MissionSpec) -> Path | None:
        pack = self._resolve_pack_dir(spec)
        if pack is not None:
            return pack.parent if pack.name == "pack" else pack
        root = Path(self._artifacts_root)
        # Open campaigns use run_dir/missions; some harnesses use run_dir/artifacts.
        if root.name in {"artifacts", "missions"}:
            return root.parent
        return None

    @staticmethod
    def _ticks_for_grant(granted: dict[str, Any], *, n_agents: int) -> int:
        # Unlimited must not silently mean a small top-up — that re-opens the
        # human gate every few thousand ticks and looks like "授权无限仍中断".
        if granted.get("unlimited"):
            return 1_000_000_000
        extra = max(1, int(granted.get("extra_iterations") or 1))
        return max(200, extra * (max(1, n_agents) + 4) * 2)

    async def _await_budget_grant(
        self,
        run_dir: Path,
        *,
        mission_id: str,
        team_run_id: str,
        reason: str,
        detail: str = "",
        ticks_used: int | None = None,
        tick_budget: int | None = None,
    ) -> dict[str, Any] | None:
        """Block until Observatory/operator decides. None ⇒ abort."""
        final = self._orchestrator.require(team_run_id)
        coord = CoordinationSpec.from_run_metadata(final.metadata)
        current_max = (
            coord.stopping.max_total_rounds
            or coord.stopping.max_iterations
            or int(final.active_round)
        )

        def _ensure_pending_gate() -> None:
            # Re-open if the control file was deleted while waiting (UI/ops race);
            # otherwise Observatory cannot show the human decision card.
            open_gate(
                run_dir,
                mission_id=mission_id,
                team_run_id=team_run_id,
                reason=reason,
                rounds_used=int(final.active_round),
                current_max_iterations=int(current_max),
                detail=detail,
                ticks_used=ticks_used,
                tick_budget=tick_budget,
            )

        _ensure_pending_gate()
        poll = float(os.environ.get("SCITEAM_BUDGET_GATE_POLL_SECONDS") or "1.5")
        poll = max(0.05, poll)
        while True:
            gate = read_gate(run_dir)
            if gate is None or gate.get("status") != "decided":
                if gate is None or gate.get("status") != "pending":
                    _ensure_pending_gate()
                await asyncio.sleep(poll)
                continue
            decision = gate.get("decision") or {}
            action = str(decision.get("action") or "")
            if action in {"grant_limited", "grant_unlimited"} and grant_forbidden(gate):
                # Appendix A: billing / seed-cost HOLD stays waiting_user.
                # An illegal grant must not extend budget.
                gate["status"] = "pending"
                gate["decision"] = None
                gate["rejected_grant"] = {
                    "action": action,
                    "note": "grant_forbidden: HOLD, do not grant",
                }
                path = run_dir / "control" / "budget_gate.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
                await asyncio.sleep(poll)
                continue
            if action == "abort":
                return None
            unlimited = action == "grant_unlimited"
            extra = int(decision.get("extra_iterations") or 0)
            # Institutional human_gate: operator grant must set the clearance
            # flag before the next assessor COMPLETED, or the run re-parks.
            if reason == "human_gate":
                peek = self._orchestrator.require(team_run_id)
                peek.metadata["human_gate_cleared"] = True
                self._orchestrator._store.save(peek)
            if reason == "budget_exhausted" or final.metadata.get("budget_exhausted"):
                self._orchestrator.extend_budget(
                    team_run_id,
                    extra_iterations=None if unlimited else extra,
                    unlimited=unlimited,
                    note=str(decision.get("operator_note") or ""),
                )
            elif unlimited or extra > 0:
                # Wall/tick ceilings: still clear WAITING_USER if present.
                peek = self._orchestrator.require(team_run_id)
                if peek.state == TeamRunState.WAITING_USER:
                    self._orchestrator.extend_budget(
                        team_run_id,
                        extra_iterations=None if unlimited else max(extra, 1),
                        unlimited=unlimited,
                        note=str(decision.get("operator_note") or ""),
                    )
            clear_gate(run_dir)
            return {
                "unlimited": unlimited,
                "extra_iterations": 0 if unlimited else max(1, extra),
                "note": str(decision.get("operator_note") or ""),
            }
