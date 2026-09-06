"""Test bootstrap: make the lab root importable regardless of invocation dir."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from sciteam import (  # noqa: E402
    AgentRunResult,
    ContractValidator,
    MissionRunner,
    Run,
    RunSpec,
    RunState,
    TeamOrchestrator,
    build_registry,
)

SCHEMAS_DIR = LAB_ROOT / "schemas"
PARADIGMS_DIR = LAB_ROOT / "assets" / "teams" / "paradigms"


class ArtifactWritingRuntime:
    """Fake worker runtime: the rostered 'worker' writes the mission artifact.

    Mirrors the real contract: workers produce artifacts, the engine only
    validates them. `artifacts_by_mission` maps mission id (or prefix before
    an `_a<N>` retry suffix) to the artifact dict to write; missing entries
    mean the worker writes nothing (contract will fail).
    """

    def __init__(self, artifacts_by_mission: dict[str, dict] | None = None) -> None:
        self.artifacts_by_mission = artifacts_by_mission or {}
        self.calls: list[str] = []

    def _artifact_for(self, mission_id: str) -> dict | None:
        if mission_id in self.artifacts_by_mission:
            return self.artifacts_by_mission[mission_id]
        base = mission_id.rsplit("_a", 1)[0]
        return self.artifacts_by_mission.get(base)

    async def run_subagent(
        self,
        *,
        agent_id: str,
        task: str,
        work_dir: str,
        parent_run_id: str | None = None,
        depth: int = 0,
        context: dict | None = None,
    ) -> AgentRunResult:
        del task, work_dir, depth
        context = context or {}
        key = str(context.get("team_agent_key") or agent_id)
        self.calls.append(key)
        meta = context.get("team_metadata") or {}
        mission_id = str(meta.get("mission_id") or "")
        artifact_path = str(meta.get("artifact_path") or "")
        artifact = self._artifact_for(mission_id)
        if artifact is not None and artifact_path:
            payload = dict(artifact)
            payload.setdefault("mission_id", mission_id)
            Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
            Path(artifact_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        run = Run(
            run_id=f"worker_{key}_{len(self.calls)}",
            spec=RunSpec(kind="team_worker", input=key, agent_id=agent_id, parent_run_id=parent_run_id),
            state=RunState.SUCCEEDED,
            output=f"ok:{key}",
        )
        return AgentRunResult(run=run, output=run.output)


@pytest.fixture
def paradigms():
    return build_registry(PARADIGMS_DIR)


@pytest.fixture
def validator():
    return ContractValidator(SCHEMAS_DIR)


@pytest.fixture
def make_runner(tmp_path, paradigms, validator):
    def _make(runtime) -> MissionRunner:
        orch = TeamOrchestrator(runtime=runtime, work_root=tmp_path / "work")
        return MissionRunner(
            orchestrator=orch,
            paradigms=paradigms,
            validator=validator,
            artifacts_root=tmp_path / "missions",
        )

    return _make
