"""Campaign loop: playbook planner, budget clock, KB, trace, re-entry (E2)."""

from __future__ import annotations

import pytest
from sciteam import (
    BudgetClock,
    Campaign,
    CampaignBudget,
    CampaignKB,
    CampaignTrace,
    ScriptedPlaybookPlanner,
)

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

VALID_ATTACK = {
    "target_claims": [{"claim_id": "c1", "statement": "candidate beats LRU"}],
    "attacks": [
        {
            "attack_id": "a1",
            "vector": "seed sensitivity",
            "method": "re-ran the frozen eval across 5 alternative seeds",
            "outcome": "survived",
        }
    ],
    "verdict": "promote",
}


def _playbook(on_fail_hyp=None, on_fail_attack=None) -> dict:
    steps = [
        {
            "mission": {
                "id": "wave_one",
                "kind": "hypothesis_atelier",
                "goal": "produce candidates",
                "paradigm": "debate_elo",
                "exit_contract": "candidate_hypotheses",
                "budget": {"max_rounds": 4},
            }
        },
        {
            "mission": {
                "id": "stress",
                "kind": "adversarial",
                "goal": "attack the claims",
                "paradigm": "adversarial_pair",
                "exit_contract": "attack_report",
                "budget": {"max_rounds": 4},
            }
        },
    ]
    if on_fail_hyp:
        steps[0]["on_fail"] = on_fail_hyp
    if on_fail_attack:
        steps[1]["on_fail"] = on_fail_attack
    return {
        "playbook_id": "pb_test",
        "steps": steps,
        "finalizer": {
            "mission": {
                "id": "final_honest",
                "kind": "wrap_up",
                "goal": "write honest partial/negative result",
                "paradigm": "write_atelier",
                "exit_contract": "package_manifest",
                "budget": {"max_rounds": 4},
            }
        },
    }


def _campaign(tmp_path, make_runner, runtime, playbook_dict, budget=None):
    runner = make_runner(runtime)
    kb = CampaignKB(tmp_path / "kb.jsonl")
    trace = CampaignTrace(tmp_path / "trace.jsonl")
    planner = ScriptedPlaybookPlanner(playbook_dict)
    return Campaign(
        campaign_id="camp_test",
        goal="test standing goal",
        planner=planner,
        runner=runner,
        kb=kb,
        trace=trace,
        budget=budget,
    )


class TestHappyPath:
    async def test_playbook_runs_to_completion(self, tmp_path, make_runner):
        runtime = ArtifactWritingRuntime(
            {"wave_one": VALID_HYPOTHESES, "stress": VALID_ATTACK}
        )
        campaign = _campaign(tmp_path, make_runner, runtime, _playbook())
        result = await campaign.run()
        assert result.stop_reason == "playbook complete"
        assert [o.mission_id for o in result.outcomes] == ["wave_one", "stress"]
        assert all(o.succeeded for o in result.outcomes)
        # KB has one mission_outcome per mission
        assert len(campaign.kb.entries("mission_outcome")) == 2
        # Trace paradigm coverage picks up both paradigms (E1 feed)
        assert set(campaign.trace.paradigms_used()) == {"debate_elo", "adversarial_pair"}
        closure = campaign.trace.contract_closure()
        assert closure["missions"] == 2 and closure["contract_failed"] == 0

    async def test_trace_exports(self, tmp_path, make_runner):
        runtime = ArtifactWritingRuntime(
            {"wave_one": VALID_HYPOTHESES, "stress": VALID_ATTACK}
        )
        campaign = _campaign(tmp_path, make_runner, runtime, _playbook())
        await campaign.run()
        files = campaign.trace.export_summaries(tmp_path / "exports")
        for key in ("paradigms_used", "contract_closure", "mission_reentries"):
            assert files[key].is_file()


class TestFailurePolicies:
    async def test_retry_reissues_with_attempt_suffix(self, tmp_path, make_runner):
        # First attempt writes invalid artifact; retry attempt writes valid one.
        runtime = ArtifactWritingRuntime(
            {
                "wave_one": {"problem_id": "p2", "candidates": []},  # invalid
                "wave_one_a2": VALID_HYPOTHESES,
                "stress": VALID_ATTACK,
            }
        )
        campaign = _campaign(
            tmp_path, make_runner, runtime, _playbook(on_fail_hyp={"action": "retry", "max_times": 2})
        )
        result = await campaign.run()
        ids = [o.mission_id for o in result.outcomes]
        assert ids == ["wave_one", "wave_one_a2", "stress"]
        assert result.outcomes[0].status == "contract_failed"
        assert result.outcomes[1].succeeded

    async def test_reenter_reopens_earlier_step_and_logs_e2(self, tmp_path, make_runner):
        # Adversarial mission fails once; policy re-enters the first step.
        artifacts = {
            "wave_one": VALID_HYPOTHESES,
            # stress fails on attempt 1 (no artifact), succeeds on re-entry pass
            "stress_a2": VALID_ATTACK,
        }
        runtime = ArtifactWritingRuntime(artifacts)
        playbook = _playbook(
            on_fail_attack={"action": "reenter", "target": "wave_one", "max_times": 2}
        )
        campaign = _campaign(tmp_path, make_runner, runtime, playbook)
        result = await campaign.run()
        ids = [o.mission_id for o in result.outcomes]
        assert ids == ["wave_one", "stress", "wave_one_a2", "stress_a2"]
        assert result.outcomes[-1].succeeded
        # E2: the planner decision that reopened wave_one is marked as re-entry
        reentries = campaign.trace.reentries()
        assert len(reentries) == 1
        assert reentries[0]["reentry_target"] == "wave_one"

    async def test_abort_triggers_finalizer(self, tmp_path, make_runner):
        runtime = ArtifactWritingRuntime(
            {
                "wave_one": VALID_HYPOTHESES,
                "final_honest": {
                    "campaign_id": "camp_test",
                    "outcome": "partial",
                    "contents": [{"kind": "mission_trace", "path": "trace.jsonl"}],
                },
            }
        )
        campaign = _campaign(
            tmp_path, make_runner, runtime, _playbook(on_fail_attack={"action": "abort"})
        )
        result = await campaign.run()
        ids = [o.mission_id for o in result.outcomes]
        assert ids == ["wave_one", "stress", "final_honest"]
        assert result.outcomes[-1].succeeded  # honest wrap-up validated


class TestBudgetClock:
    def test_exhaustion_dimensions(self):
        clock = BudgetClock(CampaignBudget(max_missions=2, max_llm_tokens=100))
        assert clock.exhausted() == ""
        clock.charge_mission()
        clock.charge_mission()
        assert clock.exhausted() == "max_missions"
        clock2 = BudgetClock(CampaignBudget(max_llm_tokens=10))
        clock2.charge_tokens(10)
        assert clock2.exhausted() == "max_llm_tokens"

    async def test_budget_exhaustion_forces_finalizer(self, tmp_path, make_runner):
        runtime = ArtifactWritingRuntime(
            {
                "wave_one": VALID_HYPOTHESES,
                "final_honest": {
                    "campaign_id": "camp_test",
                    "outcome": "partial",
                    "contents": [{"kind": "mission_trace", "path": "trace.jsonl"}],
                },
            }
        )
        campaign = _campaign(
            tmp_path,
            make_runner,
            runtime,
            _playbook(),
            budget=CampaignBudget(max_missions=1),
        )
        result = await campaign.run()
        ids = [o.mission_id for o in result.outcomes]
        assert ids == ["wave_one", "final_honest"]
        assert "budget exhausted" in result.stop_reason or "finalizer" in result.stop_reason


class TestKB:
    def test_append_only_and_reload(self, tmp_path):
        kb = CampaignKB(tmp_path / "kb.jsonl")
        kb.append("claim", {"text": "prefix reuse dominates"}, mission_id="m1")
        kb.append("citation", {"key": "smartcache2025"}, mission_id="m1")
        kb.append("claim", {"text": "eviction order matters"}, mission_id="m2")
        reloaded = CampaignKB(tmp_path / "kb.jsonl")
        assert len(reloaded.entries()) == 3
        assert len(reloaded.entries("claim")) == 2
        assert reloaded.latest("claim").mission_id == "m2"
        summary = reloaded.summary()
        assert summary["total_entries"] == 3
        assert summary["kinds"]["claim"]["count"] == 2

    def test_empty_kind_rejected(self, tmp_path):
        kb = CampaignKB(tmp_path / "kb.jsonl")
        with pytest.raises(ValueError):
            kb.append("", {})
