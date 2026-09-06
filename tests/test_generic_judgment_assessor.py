"""G0: minimal judgment team — engine must generalize beyond tournament."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sciteam import (
    AgentRunResult,
    ContractValidator,
    MissionRunner,
    MissionSpec,
    Run,
    RunSpec,
    RunState,
    TeamOrchestrator,
    build_registry,
)
from sciteam.coordinator import ContractCoordinator
from sciteam.models import TeamRunState
from sciteam.round_assessor import ScriptedAssessor
from sciteam.team_loader import load_team_config

from tests.conftest import LAB_ROOT

FIXTURE = LAB_ROOT / "tests" / "fixtures" / "teams" / "minimal_judgment.yaml"

TINY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"const": True}, "mission_id": {"type": "string"}},
    "additionalProperties": True,
}


class EmitRuntime:
    def __init__(self, *, emit: bool = True) -> None:
        self.emit = emit
        self.calls: list[str] = []

    async def run_subagent(self, **kwargs):
        key = str((kwargs.get("context") or {}).get("team_agent_key") or "x")
        self.calls.append(key)
        meta = (kwargs.get("context") or {}).get("team_metadata") or {}
        path = str(meta.get("artifact_path") or "")
        emit_roles = {str(x) for x in (meta.get("artifact_emit_roles") or [])}
        if self.emit and key in emit_roles and path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(
                json.dumps({"ok": True, "mission_id": meta.get("mission_id") or "m"}),
                encoding="utf-8",
            )
        run = Run(
            run_id=f"g0_{key}_{len(self.calls)}",
            spec=RunSpec(kind="team_worker", input=key, agent_id=key),
            state=RunState.SUCCEEDED,
            output=f"ok:{key}",
        )
        return AgentRunResult(run=run, output=run.output)


def _schemas(tmp_path: Path) -> Path:
    root = tmp_path / "schemas"
    root.mkdir()
    (root / "tiny_ok.schema.json").write_text(
        json.dumps(TINY_SCHEMA), encoding="utf-8"
    )
    return root


def test_fixture_loads_with_assess_seat():
    cfg = load_team_config(FIXTURE)
    assert "round_assessor" in cfg.assess_roles()
    assert "writer" in cfg.artifact_emit_roles()


def test_all_asset_judgment_paradigms_declare_emit_and_assess():
    """Institutional gap: judgment without emit-duty stalls forever (need_artifact)."""
    from sciteam import build_registry

    registry = build_registry(LAB_ROOT / "assets" / "teams" / "paradigms")
    for cfg in registry.list():
        pid = cfg.team_id
        stopping = (cfg.coordination or {}).get("stopping") or {}
        if str(stopping.get("kind") or "") != "judgment":
            continue
        assert cfg.assess_roles(), f"{pid}: missing may_assess_round seat"
        assert cfg.artifact_emit_roles(), f"{pid}: missing emits_exit_artifact seat"


def test_minimal_judgment_completes_with_artifact(tmp_path):
    registry = build_registry(FIXTURE.parent)
    runtime = EmitRuntime(emit=True)
    orch = TeamOrchestrator(
        runtime=runtime,
        work_root=tmp_path / "work",
        coordinator=ContractCoordinator(ScriptedAssessor()),
    )
    runner = MissionRunner(
        orchestrator=orch,
        paradigms=registry,
        validator=ContractValidator(_schemas(tmp_path)),
        artifacts_root=tmp_path / "missions",
    )
    spec = MissionSpec.from_dict(
        {
            "id": "m_min",
            "goal": "generic judgment closure",
            "paradigm": "minimal_judgment",
            "exit_contract": "tiny_ok",
            "budget": {"max_rounds": 6},
            "metadata": {
                "stagnation_max_rounds": 4,
                "artifact_gate": {"pointer": "/ok", "equals": True},
            },
        }
    )
    outcome = asyncio.run(runner.run(spec, max_ticks=40))
    run = orch.require(outcome.team_run_id)
    assert "round_assessor" not in runtime.calls
    assert runtime.calls == ["writer"] or runtime.calls[0] == "writer"
    assert outcome.status == "completed", outcome
    assert run.state == TeamRunState.COMPLETED
    assert run.metadata.get("last_assessment_reason") == "ok_complete"
    assert list(Path(run.work_root).glob("round_assessment_r*.json"))


def test_minimal_judgment_stalls_without_emit(tmp_path):
    registry = build_registry(FIXTURE.parent)
    runtime = EmitRuntime(emit=False)
    orch = TeamOrchestrator(
        runtime=runtime,
        work_root=tmp_path / "work",
        coordinator=ContractCoordinator(ScriptedAssessor()),
    )
    runner = MissionRunner(
        orchestrator=orch,
        paradigms=registry,
        validator=ContractValidator(_schemas(tmp_path)),
        artifacts_root=tmp_path / "missions",
    )
    spec = MissionSpec.from_dict(
        {
            "id": "m_stall",
            "goal": "should fail-honest",
            "paradigm": "minimal_judgment",
            "exit_contract": "tiny_ok",
            "budget": {"max_rounds": 10},
            "metadata": {"stagnation_max_rounds": 2},
        }
    )
    outcome = asyncio.run(runner.run(spec, max_ticks=40))
    run = orch.require(outcome.team_run_id)
    assert outcome.status == "failed", outcome
    assert run.state == TeamRunState.FAILED
    assert run.metadata.get("last_assessment_reason") == "exit_stalled"
    assert "round_assessor" not in runtime.calls
