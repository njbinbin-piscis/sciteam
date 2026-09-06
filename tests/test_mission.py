"""MissionSpec + exit-contract validation invariants."""

from __future__ import annotations

import pytest
from sciteam import MissionBudget, MissionSpec, MissionSpecError

from tests.conftest import ArtifactWritingRuntime

VALID_HYPOTHESES = {
    "problem_id": "p2_kvcache",
    "candidates": [
        {
            "hypothesis_id": "h1",
            "statement": "Prefix-aware eviction retains shared-prefix blocks longer than LRU under multi-session reuse.",
            "falsification_criterion": "hit rate <= max(LRU, LFU) on the frozen trace",
            "rationale": "sessions share long system prompts",
        }
    ],
}


def _spec(**overrides) -> MissionSpec:
    data = {
        "id": "m_hyp_wave_01",
        "goal": "produce falsifiable candidates",
        "paradigm": "debate_elo",
        "exit_contract": "candidate_hypotheses",
        "skills_allowlist": ["hypothesis.formulate", "lit.survey"],
        "budget": {"max_rounds": 4},
    }
    data.update(overrides)
    return MissionSpec.from_dict(data)


class TestMissionSpec:
    def test_required_fields(self):
        for missing in ("id", "goal", "paradigm", "exit_contract"):
            data = _spec().to_dict()
            data[missing] = ""
            with pytest.raises(MissionSpecError):
                MissionSpec.from_dict(data)

    def test_roundtrip(self):
        spec = _spec()
        again = MissionSpec.from_dict(spec.to_dict())
        assert again == spec

    def test_kind_is_data_not_engine_enum(self):
        # kind is a free string: arbitrary labels are accepted unchanged
        spec = _spec(kind="totally_new_research_style")
        assert spec.kind == "totally_new_research_style"

    def test_budget_parsing(self):
        budget = MissionBudget.from_dict({"max_rounds": 7, "max_wall_clock_seconds": 60})
        assert budget.max_rounds == 7
        assert budget.max_wall_clock_seconds == 60
        assert budget.max_llm_tokens == 0


class TestContractValidator:
    def test_valid_artifact_passes(self, validator):
        artifact = dict(VALID_HYPOTHESES, mission_id="m1")
        assert validator.validate("candidate_hypotheses", artifact) == []

    def test_missing_fields_fail(self, validator):
        errors = validator.validate("candidate_hypotheses", {"mission_id": "m1"})
        assert errors
        assert any("problem_id" in e or "candidates" in e for e in errors)

    def test_unknown_schema_reported(self, validator):
        errors = validator.validate("no_such_contract", {})
        assert errors and "not found" in errors[0]

    def test_results_schema_requires_frozen_hash(self, validator):
        artifact = {
            "problem_id": "p2_kvcache",
            "eval_script_sha256": "nothash",
            "seed": 1,
            "baseline": {"name": "lru", "metrics": {"hit_rate": 0.5}},
            "candidate": {"name": "cand", "metrics": {"hit_rate": 0.6}},
            "success_predicate": "hit_rate >= baseline * 1.05",
            "success": True,
            "produced_at": "2026-07-11T00:00:00Z",
        }
        errors = validator.validate("results", artifact)
        assert errors and any("eval_script_sha256" in e for e in errors)


class TestMissionRunner:
    async def test_mission_completes_when_contract_validates(self, make_runner):
        runtime = ArtifactWritingRuntime({"m_hyp_wave_01": VALID_HYPOTHESES})
        runner = make_runner(runtime)
        outcome = await runner.run(_spec())
        assert outcome.status == "completed"
        assert outcome.succeeded
        assert outcome.contract_errors == []
        assert outcome.paradigm == "debate_elo"
        # debate_elo expands 3 debater replicas + 1 judge
        assert len(runtime.calls) == 4

    async def test_contract_failed_when_worker_writes_nothing(self, make_runner):
        runtime = ArtifactWritingRuntime({})
        runner = make_runner(runtime)
        outcome = await runner.run(_spec())
        assert outcome.status == "contract_failed"
        assert not outcome.succeeded
        assert outcome.contract_errors

    async def test_contract_failed_on_invalid_artifact(self, make_runner):
        runtime = ArtifactWritingRuntime({"m_hyp_wave_01": {"problem_id": "p2", "candidates": []}})
        runner = make_runner(runtime)
        outcome = await runner.run(_spec())
        assert outcome.status == "contract_failed"
        assert any("candidates" in e for e in outcome.contract_errors)

    async def test_unknown_paradigm_rejected(self, make_runner):
        runner = make_runner(ArtifactWritingRuntime({}))
        with pytest.raises(MissionSpecError):
            await runner.run(_spec(paradigm="no_such_paradigm"))

    async def test_roster_override_replaces_template_members(self, make_runner):
        runtime = ArtifactWritingRuntime({"m_hyp_wave_01": VALID_HYPOTHESES})
        runner = make_runner(runtime)
        outcome = await runner.run(
            _spec(roster=[{"agent_key": "analyst", "agent_id": "analyst", "name": "Problem Analyst"}])
        )
        assert outcome.succeeded
        assert runtime.calls == ["analyst"]

    async def test_mission_budget_tightens_rounds(self, make_runner, paradigms, validator):
        spec = _spec(budget={"max_rounds": 1})
        runtime = ArtifactWritingRuntime({})  # never completes contract

        class NeverDoneRuntime(ArtifactWritingRuntime):
            pass

        runner = make_runner(NeverDoneRuntime({}))
        del runtime
        outcome = await runner.run(spec)
        # ScriptedCoordinator completes after one round of done tasks, but the
        # contract fails; the important invariant is rounds never exceed budget.
        assert outcome.rounds_used <= 1


class TestContractCoordinatorGate:
    """The artifact gate is a generic data-driven completion condition."""

    @staticmethod
    def _run_with_artifact(tmp_path, artifact: dict | None, gate: dict | None):
        import json as _json

        from sciteam.models import AgentSlot, Task, TaskState, TeamRun, TeamRunState

        artifact_path = tmp_path / "results.json"
        if artifact is not None:
            artifact_path.write_text(_json.dumps(artifact), encoding="utf-8")
        metadata = {"artifact_path": str(artifact_path)}
        if gate is not None:
            metadata["artifact_gate"] = gate
        return TeamRun(
            id="tr1",
            goal="g",
            team_profile_id="p",
            state=TeamRunState.RUNNING,
            active_round=1,
            agents=[AgentSlot(agent_key="w", profile={}, work_dir=str(tmp_path))],
            tasks=[
                Task(
                    task_id="t1",
                    agent_key="w",
                    prompt="p",
                    state=TaskState.DONE,
                    result="ok",
                    round_index=1,
                )
            ],
            work_root=str(tmp_path),
            metadata=metadata,
        )

    async def test_gate_pass_completes(self, tmp_path):
        from sciteam.coordinator import ContractCoordinator
        from sciteam.models import AssessmentDecision
        from sciteam.round_assessor import ScriptedAssessor

        run = self._run_with_artifact(
            tmp_path, {"success": True}, {"pointer": "/success", "equals": True}
        )
        assessment = await ContractCoordinator(ScriptedAssessor()).assess_round(run)
        assert assessment.decision == AssessmentDecision.COMPLETED
        assert assessment.reason_code == "ok_complete"

    async def test_gate_fail_continues_with_hint(self, tmp_path):
        from sciteam.coordinator import ContractCoordinator
        from sciteam.models import AssessmentDecision
        from sciteam.round_assessor import ScriptedAssessor

        run = self._run_with_artifact(
            tmp_path, {"success": False}, {"pointer": "/success", "equals": True}
        )
        assessment = await ContractCoordinator(ScriptedAssessor()).assess_round(run)
        assert assessment.decision == AssessmentDecision.CONTINUE
        assert assessment.reason_code == "gate_unsatisfied"
        assert "gate" in (assessment.next_round_hint or "")

    async def test_gate_missing_pointer_continues(self, tmp_path):
        from sciteam.coordinator import ContractCoordinator
        from sciteam.models import AssessmentDecision
        from sciteam.round_assessor import ScriptedAssessor

        run = self._run_with_artifact(
            tmp_path, {"other": 1}, {"pointer": "/success", "equals": True}
        )
        assessment = await ContractCoordinator(ScriptedAssessor()).assess_round(run)
        assert assessment.decision == AssessmentDecision.CONTINUE
        assert assessment.reason_code == "gate_unsatisfied"

    async def test_no_gate_completes_on_artifact_presence(self, tmp_path):
        from sciteam.coordinator import ContractCoordinator
        from sciteam.models import AssessmentDecision
        from sciteam.round_assessor import ScriptedAssessor

        run = self._run_with_artifact(tmp_path, {"success": False}, None)
        assessment = await ContractCoordinator(ScriptedAssessor()).assess_round(run)
        assert assessment.decision == AssessmentDecision.COMPLETED

    async def test_missing_artifact_continues_names_duty_role(self, tmp_path):
        from sciteam.coordinator import ContractCoordinator
        from sciteam.models import AssessmentDecision
        from sciteam.round_assessor import ScriptedAssessor

        run = self._run_with_artifact(tmp_path, None, None)
        run.metadata["artifact_emit_roles"] = ["writer"]
        assessment = await ContractCoordinator(ScriptedAssessor()).assess_round(run)
        assert assessment.decision == AssessmentDecision.CONTINUE
        assert assessment.reason_code == "need_artifact"
        assert "writer" in (assessment.next_round_hint or "")

    async def test_missing_artifact_promotes_valid_draft(self, tmp_path):
        import json as _json

        from sciteam.coordinator import ContractCoordinator
        from sciteam.models import AssessmentDecision
        from sciteam.round_assessor import ScriptedAssessor

        from tests.conftest import LAB_ROOT

        draft = {
            "mission_id": "m_hyp",
            "problem_id": "p1",
            "candidates": [
                {
                    "hypothesis_id": "H1",
                    "statement": "A sufficiently long falsifiable claim here.",
                    "falsification_criterion": "If assay X fails, kill H1.",
                    "rationale": "Grounded in literature Y.",
                    "smoke_check": None,
                    "_meta_review_notes": {"x": 1},
                }
            ],
        }
        (tmp_path / "candidate_hypotheses_draft.json").write_text(
            _json.dumps(draft), encoding="utf-8"
        )
        run = self._run_with_artifact(tmp_path, None, {"pointer": "/mission_id", "equals": "m_hyp"})
        # Point artifact_path at canonical name used by promote
        run.metadata["artifact_path"] = str(tmp_path / "candidate_hypotheses.json")
        run.metadata["exit_contract"] = "candidate_hypotheses"
        run.metadata["schemas_dir"] = str(LAB_ROOT / "schemas")
        run.metadata["artifact_emit_roles"] = ["writer"]
        assessment = await ContractCoordinator(
            ScriptedAssessor({"promote_if_draft": True, "assessor_key": "round_assessor"})
        ).assess_round(run)
        assert (tmp_path / "candidate_hypotheses.json").is_file()
        written = _json.loads((tmp_path / "candidate_hypotheses.json").read_text())
        assert "_meta_review_notes" not in written["candidates"][0]
        assert assessment.decision == AssessmentDecision.COMPLETED
