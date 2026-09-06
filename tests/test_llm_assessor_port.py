"""LlmAssessorPort loads L2 assets and returns parsed envelopes."""

from __future__ import annotations

import asyncio
import json

from sciteam.coordinator import ContractCoordinator
from sciteam.models import AgentSlot, Task, TaskState, TeamRun, TeamRunState
from sciteam.round_assessor import LlmAssessorPort, ScriptedAssessor, make_contract_coordinator

from tests.conftest import LAB_ROOT


class _FakeRuntime:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict] = []

    async def run_subagent(self, **kwargs):
        self.calls.append(kwargs)
        from sciteam.runtime import AgentRunResult, Run, RunSpec, RunState

        run = Run(
            run_id="a1",
            spec=RunSpec(kind="team_worker", input="x", agent_id="round_assessor"),
            state=RunState.SUCCEEDED,
            output=self.output,
        )
        return AgentRunResult(run=run, output=self.output)


def test_llm_assessor_loads_role_prompt_and_parses_envelope():
    envelope = {
        "decision": "continue",
        "reason_code": "need_artifact",
        "summary": "missing exit",
        "next_round_hint": "emit seat must write file",
        "assessor_key": "round_assessor",
    }
    runtime = _FakeRuntime(json.dumps(envelope))
    port = LlmAssessorPort(runtime=runtime, assets_root=LAB_ROOT / "assets")
    run = TeamRun(
        id="r",
        goal="g",
        team_profile_id="p",
        state=TeamRunState.RUNNING,
        active_round=1,
        agents=[
            AgentSlot(
                agent_key="round_assessor",
                profile={"role": "round_assessor", "may_assess_round": True},
                work_dir=str(LAB_ROOT),
            )
        ],
        tasks=[],
        work_root=str(LAB_ROOT),
        metadata={"assess_roles": ["round_assessor"]},
    )
    facts = {
        "assess_roles": ["round_assessor"],
        "artifact_exists": False,
        "active_round": 1,
    }
    raw = asyncio.run(port.assess(facts, run))
    assert raw["decision"] == "continue"
    assert raw["reason_code"] == "need_artifact"
    assert runtime.calls
    task = runtime.calls[0]["task"]
    assert "Round Assessor" in task or "round_assessor" in task.lower()
    assert "institution.round_assess" in task or "Observation pack" in task


class _ExplodingAssessor:
    """Any consult proves the coordinator broke verification-kind semantics."""

    async def assess(self, facts, run):
        raise AssertionError("LLM assessor must not be consulted for verification-kind stopping")


def _verification_run(tmp_path, *, artifact_path: str) -> TeamRun:
    return TeamRun(
        id="r",
        goal="g",
        team_profile_id="p",
        state=TeamRunState.RUNNING,
        active_round=1,
        agents=[
            AgentSlot(
                agent_key="builder",
                profile={"role": "builder"},
                work_dir=str(tmp_path),
            )
        ],
        tasks=[
            Task(
                task_id="t1",
                agent_key="builder",
                prompt="build",
                state=TaskState.DONE,
                result="ok",
                round_index=1,
            )
        ],
        work_root=str(tmp_path),
        metadata={
            "coordination": {"stopping": {"kind": "verification", "max_iterations": 4}},
            "artifact_path": artifact_path,
        },
    )


def test_verification_kind_completes_from_verifier_artifact(tmp_path):
    artifact = tmp_path / "results.json"
    artifact.write_text('{"status": "pass"}\n', encoding="utf-8")
    coordinator = ContractCoordinator(assessor=_ExplodingAssessor())
    run = _verification_run(tmp_path, artifact_path=str(artifact))
    verdict = asyncio.run(coordinator.assess_round(run))
    assert verdict.decision.value == "completed"
    assert verdict.reason_code == "ok_complete"


def test_verification_kind_continues_without_artifact(tmp_path):
    coordinator = ContractCoordinator(assessor=_ExplodingAssessor())
    run = _verification_run(tmp_path, artifact_path=str(tmp_path / "results.json"))
    verdict = asyncio.run(coordinator.assess_round(run))
    assert verdict.decision.value == "continue"
    assert verdict.reason_code == "need_artifact"


def test_make_contract_coordinator_modes():
    fake = make_contract_coordinator(live=False)
    assert isinstance(fake, ContractCoordinator)
    assert isinstance(fake._assessor, ScriptedAssessor)

    runtime = _FakeRuntime("{}")
    live = make_contract_coordinator(live=True, runtime=runtime, assets_root=LAB_ROOT / "assets")
    assert isinstance(live._assessor, LlmAssessorPort)
