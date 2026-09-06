"""Human budget-gate: file protocol + mission resume after grant."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sciteam import ContractValidator, build_registry
from sciteam.budget_gate import (
    check_seed_fuse,
    decide_gate,
    gate_enabled,
    grant_forbidden,
    is_billing_failure,
    open_gate,
    pending_summary,
    read_gate,
)
from sciteam.coordinator import ContractCoordinator
from sciteam.mission import MissionRunner, MissionSpec
from sciteam.models import TeamRunState
from sciteam.orchestrator import CreateSpec, TeamOrchestrator

from tests.conftest import PARADIGMS_DIR, SCHEMAS_DIR, ArtifactWritingRuntime

VALID_HYPOTHESES = {
    "problem_id": "p2_kvcache",
    "candidates": [
        {
            "hypothesis_id": "h1",
            "statement": "Prefix-aware eviction retains shared-prefix blocks longer than LRU.",
            "falsification_criterion": "hit rate <= max(LRU, LFU)",
            "rationale": "shared prompts",
        }
    ],
}


class TestBudgetGateProtocol:
    def test_open_decide_abort(self, tmp_path):
        open_gate(
            tmp_path,
            mission_id="m_write",
            team_run_id="tr1",
            reason="budget_exhausted",
            rounds_used=48,
            current_max_iterations=48,
        )
        assert pending_summary(tmp_path)["pending"] is True
        decided = decide_gate(tmp_path, action="abort")
        assert decided["status"] == "decided"
        assert decided["decision"]["action"] == "abort"
        assert pending_summary(tmp_path) is None

    def test_grant_limited_requires_extra(self, tmp_path):
        open_gate(
            tmp_path,
            mission_id="m1",
            team_run_id="tr1",
            reason="budget_exhausted",
            rounds_used=2,
            current_max_iterations=2,
        )
        with pytest.raises(ValueError):
            decide_gate(tmp_path, action="grant_limited", extra_iterations=0)

    def test_gate_enabled_env(self, monkeypatch):
        monkeypatch.setenv("SCITEAM_BUDGET_GATE", "0")
        assert gate_enabled() is False
        monkeypatch.setenv("SCITEAM_BUDGET_GATE", "1")
        assert gate_enabled() is True


class TestTicksForGrant:
    def test_unlimited_is_not_a_tiny_topup(self):
        from sciteam.mission import MissionRunner

        n = MissionRunner._ticks_for_grant({"unlimited": True}, n_agents=6)
        assert n >= 1_000_000
        limited = MissionRunner._ticks_for_grant(
            {"unlimited": False, "extra_iterations": 100}, n_agents=6
        )
        assert limited == 100 * (6 + 4) * 2


class TestOrchestratorExtendBudget:
    async def test_extend_clears_waiting_and_raises_ceiling(self, tmp_path):
        runtime = ArtifactWritingRuntime({})
        orch = TeamOrchestrator(runtime=runtime, work_root=tmp_path / "work")
        run = orch.create_run(
            CreateSpec(
                team_profile_id="t",
                goal="g",
                agents=[{"agent_key": "w", "agent_id": "w", "name": "W"}],
                coordination={"stopping": {"max_iterations": 2}},
            )
        )
        run.state = TeamRunState.WAITING_USER
        run.active_round = 2
        run.metadata["budget_exhausted"] = True
        run.metadata["gate_reason"] = "budget_exhausted"
        orch._store.save(run)

        updated = orch.extend_budget(run.id, extra_iterations=10)
        assert updated.state == TeamRunState.RUNNING
        assert "budget_exhausted" not in updated.metadata
        stopping = updated.metadata["coordination"]["stopping"]
        assert stopping["max_iterations"] >= 12

    async def test_extend_sets_human_gate_cleared(self, tmp_path):
        """Observatory grant must clear institutional human_gate, not only budget."""
        runtime = ArtifactWritingRuntime({})
        orch = TeamOrchestrator(runtime=runtime, work_root=tmp_path / "work")
        run = orch.create_run(
            CreateSpec(
                team_profile_id="t",
                goal="g",
                agents=[{"agent_key": "w", "agent_id": "w", "name": "W"}],
                coordination={"stopping": {"max_iterations": 2}, "human_gate": True},
            )
        )
        run.state = TeamRunState.WAITING_USER
        run.active_round = 1
        run.metadata["gate_reason"] = "human_gate"
        run.metadata["human_gate_pending"] = True
        orch._store.save(run)

        updated = orch.extend_budget(run.id, extra_iterations=2, note="operator grant")
        assert updated.state == TeamRunState.RUNNING
        assert updated.metadata.get("human_gate_cleared") is True
        assert "human_gate_pending" not in updated.metadata
        assert updated.metadata.get("gate_reason") is None


class TestMissionBudgetGateResume:
    async def test_grant_limited_resumes_and_completes(self, tmp_path, monkeypatch):
        pytest.skip("legacy human-grant path; live research auto-extends task iterations")
        monkeypatch.setenv("SCITEAM_BUDGET_GATE", "1")
        monkeypatch.setenv("SCITEAM_BUDGET_GATE_POLL_SECONDS", "0.05")
        monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
        monkeypatch.delenv("SCITEAM_PACK_DIR", raising=False)

        run_dir = tmp_path
        artifacts = run_dir / "artifacts"
        work = run_dir / "work"
        artifacts.mkdir()

        runtime = ArtifactWritingRuntime({})  # no artifact until operator writes one
        orch = TeamOrchestrator(
            runtime=runtime,
            work_root=work,
            coordinator=ContractCoordinator(),
        )
        runner = MissionRunner(
            orchestrator=orch,
            paradigms=build_registry(PARADIGMS_DIR),
            validator=ContractValidator(SCHEMAS_DIR),
            artifacts_root=artifacts,
        )
        spec = MissionSpec.from_dict(
            {
                "id": "m_hyp_wave_01",
                "goal": "produce falsifiable candidates",
                "paradigm": "debate_elo",
                "exit_contract": "candidate_hypotheses",
                "skills_allowlist": ["hypothesis.formulate"],
                "budget": {"max_rounds": 1},
                "roster": [{"agent_key": "worker", "agent_id": "worker", "name": "Worker"}],
            }
        )

        async def operator():
            for _ in range(200):
                gate = pending_summary(run_dir)
                if gate and gate.get("pending"):
                    decide_gate(run_dir, action="grant_limited", extra_iterations=5)
                    final = orch.require(gate["team_run_id"])
                    path = Path(str((final.metadata or {}).get("artifact_path") or ""))
                    if path:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        payload = dict(VALID_HYPOTHESES, mission_id="m_hyp_wave_01")
                        path.write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    return
                await asyncio.sleep(0.05)
            raise TimeoutError("budget gate never opened")

        op_task = asyncio.create_task(operator())
        outcome = await runner.run(spec)
        await op_task
        assert outcome.status == "completed"
        assert outcome.succeeded
        assert outcome.rounds_used >= 1

    async def test_gate_disabled_fails_immediately(self, tmp_path, monkeypatch):
        pytest.skip("initial task rounds are no longer a research stop condition")
        monkeypatch.setenv("SCITEAM_BUDGET_GATE", "0")
        monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        orch = TeamOrchestrator(
            runtime=ArtifactWritingRuntime({}),
            work_root=tmp_path / "work",
            coordinator=ContractCoordinator(),
        )
        runner = MissionRunner(
            orchestrator=orch,
            paradigms=build_registry(PARADIGMS_DIR),
            validator=ContractValidator(SCHEMAS_DIR),
            artifacts_root=artifacts,
        )
        outcome = await runner.run(
            MissionSpec.from_dict(
                {
                    "id": "m_hyp_wave_01",
                    "goal": "g",
                    "paradigm": "debate_elo",
                    "exit_contract": "candidate_hypotheses",
                    "skills_allowlist": ["hypothesis.formulate"],
                    "budget": {"max_rounds": 1},
                    "roster": [{"agent_key": "worker", "agent_id": "worker", "name": "Worker"}],
                }
            )
        )
        assert outcome.status == "budget_exhausted"
        assert read_gate(tmp_path) is None


def test_tick_soft_refill_does_not_open_gate(tmp_path, monkeypatch):
    pytest.skip("continuation is now assessor-owned; no fixed task-round stop is asserted here")
    """Paper default: soft ticks — round/wall/contract stop; tick ceiling does not."""
    import asyncio

    monkeypatch.delenv("SCITEAM_TICK_HARD_GATE", raising=False)
    monkeypatch.setenv("SCITEAM_BUDGET_GATE", "0")
    monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
    artifacts = tmp_path / "missions"
    artifacts.mkdir()
    orch = TeamOrchestrator(
        runtime=ArtifactWritingRuntime({}),
        work_root=tmp_path / "work",
        coordinator=ContractCoordinator(),
    )
    runner = MissionRunner(
        orchestrator=orch,
        paradigms=build_registry(PARADIGMS_DIR),
        validator=ContractValidator(SCHEMAS_DIR),
        artifacts_root=artifacts,
    )
    spec = MissionSpec.from_dict(
        {
            "id": "m_hyp_wave_01",
            "goal": "g",
            "paradigm": "debate_elo",
            "exit_contract": "candidate_hypotheses",
            "skills_allowlist": ["hypothesis.formulate"],
            "budget": {"max_rounds": 1},
            "roster": [{"agent_key": "worker", "agent_id": "worker", "name": "Worker"}],
        }
    )
    outcome = asyncio.run(asyncio.wait_for(runner.run(spec, max_ticks=None), timeout=20))
    assert (read_gate(tmp_path) or {}).get("reason") != "tick_budget_exhausted"
    assert "tick budget exhausted" not in (outcome.detail or "")
    assert "tick hard-stop" not in (outcome.detail or "")
    # Real scientific stop (or contract path) — not an opaque tick fail.
    assert outcome.status in {
        "budget_exhausted",
        "completed",
        "contract_failed",
        "waiting_user",
        "failed",
    }


class TestBillingHoldAndSeedFuse:
    def test_402_diagnosis_is_billing_failure(self):
        assert is_billing_failure("Error code: 402 - Insufficient Balance")
        assert is_billing_failure("HTTP 402 from provider")
        assert not is_billing_failure("worker asked for more rounds")

    def test_402_hold_refuses_grant(self, tmp_path):
        open_gate(
            tmp_path,
            mission_id="m_verify",
            team_run_id="tr1",
            reason="worker_escalate",
            rounds_used=1,
            current_max_iterations=3,
            detail="provider HTTP 402 Insufficient Balance; retrying is pointless",
        )
        gate = read_gate(tmp_path)
        assert gate["reason"] == "billing_402"
        assert gate["hold"] is True
        assert grant_forbidden(gate)
        with pytest.raises(RuntimeError, match="HOLD"):
            decide_gate(tmp_path, action="grant_unlimited")
        with pytest.raises(RuntimeError, match="HOLD"):
            decide_gate(tmp_path, action="grant_limited", extra_iterations=2)
        decided = decide_gate(tmp_path, action="abort")
        assert decided["decision"]["action"] == "abort"

    def test_seed_fuse_token_then_wall(self):
        assert (
            check_seed_fuse(tokens=101, wall_seconds=1.0, token_cap=100, wall_cap_s=3600.0)
            == "token_fuse"
        )
        assert (
            check_seed_fuse(tokens=10, wall_seconds=10.0, token_cap=100, wall_cap_s=5.0)
            == "wall_fuse"
        )
        assert check_seed_fuse(tokens=10, wall_seconds=1.0, token_cap=100, wall_cap_s=5.0) is None

    def test_token_fuse_gate_refuses_grant(self, tmp_path):
        open_gate(
            tmp_path,
            mission_id="m1",
            team_run_id="tr1",
            reason="token_fuse",
            rounds_used=2,
            current_max_iterations=4,
            detail="seed tokens 1_200_000 > cap 1_000_000",
        )
        assert grant_forbidden(read_gate(tmp_path))
        with pytest.raises(RuntimeError, match="HOLD"):
            decide_gate(tmp_path, action="grant_limited", extra_iterations=1)
