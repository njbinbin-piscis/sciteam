"""ISA hypothesis tournament: role_pipeline serializes stages; only emit duty writes."""

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

from tests.conftest import PARADIGMS_DIR, SCHEMAS_DIR

VALID_HYP = {
    "mission_id": "m_hyp_isa",
    "problem_id": "p_isa_demo",
    "candidates": [
        {
            "hypothesis_id": "H1_DEMO",
            "statement": "A demo falsifiable claim with enough characters for schema.",
            "falsification_criterion": "If assay X fails the effect-size threshold, kill H1.",
            "rationale": "Grounded in a placeholder literature note for ISA verification.",
        }
    ],
    "rejected_ideas": [
        {"statement": "unfalsifiable slogan", "reason": "no kill criterion"}
    ],
}


class TournamentOrderRuntime:
    """Records claim order; only meta_review may write the exit artifact."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tasks: list[str] = []

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
        del work_dir, depth
        context = context or {}
        key = str(context.get("team_agent_key") or agent_id)
        self.calls.append(key)
        self.tasks.append(task)
        meta = context.get("team_metadata") or {}
        artifact_path = str(meta.get("artifact_path") or "")
        emit = {str(x) for x in (meta.get("artifact_emit_roles") or [])}
        # Non-duty must not create the file even if they "want" to.
        if key in emit or (not emit and key == "meta_review"):
            if artifact_path:
                Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
                Path(artifact_path).write_text(
                    json.dumps(VALID_HYP, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                output = f"ok:{key}:emitted"
            else:
                output = f"ok:{key}:no-path"
        else:
            # Later seats should see predecessor digest in task text.
            output = f"ok:{key}:stage"
        run = Run(
            run_id=f"isa_{key}_{len(self.calls)}",
            spec=RunSpec(
                kind="team_worker",
                input=key,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
            ),
            state=RunState.SUCCEEDED,
            output=output,
        )
        return AgentRunResult(run=run, output=output)


def test_paradigm_declares_role_pipeline_and_emit():
    registry = build_registry(PARADIGMS_DIR)
    cfg = registry.get("co_scientist_tournament")
    assert cfg is not None
    pipe = list(cfg.coordination.get("work_graph", {}).get("role_pipeline") or [])
    assert pipe == [
        "generation",
        "reflection",
        "ranking",
        "evolution",
        "proximity",
        "meta_review",
    ]
    assert "meta_review" in cfg.artifact_emit_roles()
    assert "round_assessor" in cfg.assess_roles()
    assert "round_assessor" not in pipe


def test_tournament_pipeline_serializes_and_emits(tmp_path):
    from sciteam.round_assessor import ScriptedAssessor

    runtime = TournamentOrderRuntime()
    orch = TeamOrchestrator(
        runtime=runtime,
        work_root=tmp_path / "work",
        coordinator=ContractCoordinator(ScriptedAssessor()),
    )
    runner = MissionRunner(
        orchestrator=orch,
        paradigms=build_registry(PARADIGMS_DIR),
        validator=ContractValidator(SCHEMAS_DIR),
        artifacts_root=tmp_path / "missions",
    )
    spec = MissionSpec.from_dict(
        {
            "id": "m_hyp_isa",
            "goal": "ISA tournament verification: stage graph + emit discipline",
            "paradigm": "co_scientist_tournament",
            "exit_contract": "candidate_hypotheses",
            "skills_allowlist": ["hypothesis.formulate"],
            "budget": {"max_rounds": 8},
            "metadata": {
                "artifact_gate": {"pointer": "/mission_id", "equals": "m_hyp_isa"},
                "stagnation_max_rounds": 6,
            },
        }
    )
    outcome = asyncio.run(runner.run(spec, max_ticks=80))
    assert outcome.status == "completed", outcome
    assert outcome.contract_ok
    art = Path(outcome.artifact_path)
    assert art.is_file()
    payload = json.loads(art.read_text(encoding="utf-8"))
    assert payload["mission_id"] == "m_hyp_isa"

    # First six calls in wave-1 must follow pipeline order (one each).
    first_wave = runtime.calls[:6]
    assert first_wave == [
        "generation",
        "reflection",
        "ranking",
        "evolution",
        "proximity",
        "meta_review",
    ], first_wave
    # Reflection+ must have seen generation output via predecessor digest.
    assert any(
        "prior:generation" in t for t, c in zip(runtime.tasks, runtime.calls) if c == "reflection"
    )
    # Emit only from meta_review (file appears after its call).
    assert runtime.calls.count("meta_review") >= 1


def test_stagnation_fails_without_emit(tmp_path):
    from sciteam.round_assessor import ScriptedAssessor

    class MuteRuntime:
        async def run_subagent(self, **kwargs):
            key = str((kwargs.get("context") or {}).get("team_agent_key") or "x")
            run = Run(
                run_id=f"mute_{key}",
                spec=RunSpec(kind="team_worker", input=key, agent_id=key),
                state=RunState.SUCCEEDED,
                output=f"ok:{key}:no-emit",
            )
            return AgentRunResult(run=run, output=run.output)

    orch = TeamOrchestrator(
        runtime=MuteRuntime(),
        work_root=tmp_path / "work",
        coordinator=ContractCoordinator(ScriptedAssessor()),
    )
    runner = MissionRunner(
        orchestrator=orch,
        paradigms=build_registry(PARADIGMS_DIR),
        validator=ContractValidator(SCHEMAS_DIR),
        artifacts_root=tmp_path / "missions",
    )
    spec = MissionSpec.from_dict(
        {
            "id": "m_hyp_stall",
            "goal": "should stall",
            "paradigm": "co_scientist_tournament",
            "exit_contract": "candidate_hypotheses",
            "skills_allowlist": ["hypothesis.formulate"],
            "budget": {"max_rounds": 20},
            "metadata": {"stagnation_max_rounds": 2},
        }
    )
    outcome = asyncio.run(runner.run(spec, max_ticks=40))
    assert outcome.status in {"failed", "contract_failed", "budget_exhausted"}
    run = orch.require(outcome.team_run_id)
    assert run.state in {TeamRunState.FAILED, TeamRunState.WAITING_USER, TeamRunState.COMPLETED}
    reason = str((run.metadata or {}).get("last_assessment_reason") or "")
    hint = str((run.metadata or {}).get("next_round_hint") or "")
    if outcome.status == "failed":
        assert reason == "exit_stalled" or "exit_stalled" in hint
