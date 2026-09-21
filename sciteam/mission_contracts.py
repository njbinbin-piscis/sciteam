"""Mission contracts: what a mission *is*, and what makes its output valid.

Split out of ``mission.py`` along the seam between declaration and
execution. Nothing here runs a mission; it defines the mission spec and
budget, the exit-contract validator, the outcome record, and the two pure
helpers that derive coordination/skill facts from a spec.

``mission`` re-exports every public name here, so existing imports
(``from sciteam.mission import ContractValidator``) keep working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionOutcome:
        """Rebuild an outcome from its recorded form.

        Used when resuming: a mission that already completed is restored
        from the ledger rather than run again.
        """
        return cls(
            mission_id=str(data["mission_id"]),
            status=str(data["status"]),
            team_run_id=str(data.get("team_run_id") or ""),
            rounds_used=int(data.get("rounds_used") or 0),
            wall_clock_seconds=float(data.get("wall_clock_seconds") or 0.0),
            contract_ok=bool(data.get("contract_ok")),
            contract_errors=list(data.get("contract_errors") or []),
            artifact_path=str(data.get("artifact_path") or ""),
            paradigm=str(data.get("paradigm") or ""),
            detail=str(data.get("detail") or ""),
        )


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
