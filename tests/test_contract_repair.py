"""P1-7: schema-validator-guided envelope repair for artifact-duty seats.

Replaces the previous "blind retry once" (a generic "invalid JSON, try
again" with no information about *what* was wrong) with a targeted repair
that quotes the engine's own `ContractValidator` error text, so the model
gets one concrete instruction per violation instead of guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam.llm import LlmResponse, LlmUsage
from sciteam.llm_worker import LlmWorkerRuntime, PromptAssets

from tests.conftest import LAB_ROOT

GOOD_ARTIFACT = {
    "mission_id": "m1",
    "problem_id": "p1",
    "candidates": [
        {
            "hypothesis_id": "h1",
            "statement": "x" * 25,
            "falsification_criterion": "y" * 15,
            "rationale": "z" * 15,
        }
    ],
}

# Missing the required `problem_id` key -> exactly one schema violation.
BAD_ARTIFACT = {k: v for k, v in GOOD_ARTIFACT.items() if k != "problem_id"}


class _Client:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []
        self.usage = LlmUsage()

    async def acomplete(self, messages, **_kwargs):
        self.requests.append(messages)
        return LlmResponse(content=self.responses.pop(0), usage={})


def _context(root: Path) -> dict:
    return {
        "team_run_id": "run",
        "team_agent_key": "writer",
        "work_item_id": "wi1",
        "round_index": 1,
        "team_metadata": {
            "mission_id": "m1",
            "exit_contract": "candidate_hypotheses",
            "skills_allowlist": [],
            "artifact_path": str(root / "artifact.json"),
            "artifact_emit_roles": ["writer"],
        },
    }


def _runtime(responses: list[str]) -> tuple[LlmWorkerRuntime, _Client]:
    client = _Client(responses)
    runtime = LlmWorkerRuntime(
        client=client,
        assets=PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
        ),
        schemas_dir=LAB_ROOT / "schemas",
    )
    return runtime, client


def _envelope(artifact: dict) -> str:
    return json.dumps({"summary": "draft", "facts": [], "artifact": artifact})


class TestRepairContractViolationsUnit:
    """Direct unit tests against `_repair_contract_violations`."""

    async def test_valid_artifact_returns_immediately_no_llm_call(self, tmp_path):
        runtime, client = _runtime([])
        inner_loop: list[dict] = []
        envelope = {"summary": "ok", "artifact": GOOD_ARTIFACT}
        result = await runtime._repair_contract_violations(
            [],
            envelope=envelope,
            exit_contract="candidate_hypotheses",
            inner_loop=inner_loop,
        )
        assert result == envelope
        assert not client.requests
        assert inner_loop == []

    async def test_missing_field_is_repaired_using_validator_errors(self, tmp_path):
        runtime, client = _runtime([_envelope(GOOD_ARTIFACT)])
        inner_loop: list[dict] = []
        envelope = {"summary": "draft", "artifact": BAD_ARTIFACT}
        result = await runtime._repair_contract_violations(
            [{"role": "system", "content": "sys"}],
            envelope=envelope,
            exit_contract="candidate_hypotheses",
            inner_loop=inner_loop,
        )
        assert result["artifact"] == GOOD_ARTIFACT
        assert len(client.requests) == 1
        # The repair prompt must actually name the violated field, not a
        # generic "invalid JSON" message.
        repair_prompt = str(client.requests[0][-1]["content"])
        assert "problem_id" in repair_prompt
        assert "candidate_hypotheses" in repair_prompt

        attempts = [row for row in inner_loop if row["kind"] == "contract_repair"]
        assert len(attempts) == 2  # attempt 0 (violation found) + attempt 1 (resolved)
        assert "resolved" not in attempts[0]
        assert any("problem_id" in e for e in attempts[0]["errors"])
        assert attempts[1]["resolved"] is True

    async def test_exhausts_attempts_and_returns_last_envelope(self, tmp_path):
        """If the model keeps failing the same violation, give up after the
        bounded number of attempts and hand back the last (still-invalid)
        envelope — the engine's own contract check still has the final say."""
        still_bad = _envelope(BAD_ARTIFACT)
        runtime, client = _runtime([still_bad, still_bad])
        inner_loop: list[dict] = []
        envelope = {"summary": "draft", "artifact": BAD_ARTIFACT}
        result = await runtime._repair_contract_violations(
            [{"role": "system", "content": "sys"}],
            envelope=envelope,
            exit_contract="candidate_hypotheses",
            inner_loop=inner_loop,
        )
        assert result["artifact"] == BAD_ARTIFACT
        assert len(client.requests) == 2  # bounded by _MAX_CONTRACT_REPAIR_ATTEMPTS

        attempts = [row for row in inner_loop if row["kind"] == "contract_repair"]
        assert attempts[-1]["resolved"] is False
        assert attempts[-1]["exhausted"] is True

    async def test_non_json_repair_reply_stops_early(self, tmp_path):
        runtime, client = _runtime(["not json at all"])
        inner_loop: list[dict] = []
        envelope = {"summary": "draft", "artifact": BAD_ARTIFACT}
        result = await runtime._repair_contract_violations(
            [{"role": "system", "content": "sys"}],
            envelope=envelope,
            exit_contract="candidate_hypotheses",
            inner_loop=inner_loop,
        )
        assert result["artifact"] == BAD_ARTIFACT  # kept the pre-repair envelope
        assert len(client.requests) == 1  # did not burn the second attempt
        assert inner_loop[-1]["exhausted"] is True

    async def test_non_dict_artifact_is_left_untouched(self, tmp_path):
        runtime, client = _runtime([])
        inner_loop: list[dict] = []
        envelope = {"summary": "draft", "artifact": None}
        result = await runtime._repair_contract_violations(
            [],
            envelope=envelope,
            exit_contract="candidate_hypotheses",
            inner_loop=inner_loop,
        )
        assert result is envelope
        assert not client.requests


class TestRepairWiredIntoRunSubagent:
    @pytest.mark.asyncio
    async def test_run_subagent_repairs_schema_violation_end_to_end(self, tmp_path):
        good = _envelope(GOOD_ARTIFACT)
        bad = _envelope(BAD_ARTIFACT)
        runtime, client = _runtime([bad, good])
        result = await runtime.run_subagent(
            agent_id="writer",
            task="produce candidate hypotheses",
            work_dir=str(tmp_path / "seat"),
            context=_context(tmp_path),
        )
        assert result.outcome.value == "ok"
        artifact = json.loads((tmp_path / "artifact.json").read_text(encoding="utf-8"))
        assert artifact["problem_id"] == "p1"
        # One initial completion + one contract-repair round-trip.
        assert len(client.requests) == 2

    @pytest.mark.asyncio
    async def test_run_subagent_skips_repair_when_not_artifact_duty(self, tmp_path):
        """A seat not on `artifact_emit_roles` never gets the repair
        round-trip even if it hands back a schema-violating artifact."""
        bad = _envelope(BAD_ARTIFACT)
        # Two calls even here: the tool-capable round-trip (proposal, then a
        # tools-off finalize — see LlmWorkerRuntime._complete_with_tools)
        # happens for every seat regardless of artifact duty. What must
        # NOT happen is a *third*, schema-repair call — that is the
        # behavior this test guards.
        runtime, client = _runtime([bad, bad])
        ctx = _context(tmp_path)
        ctx["team_metadata"]["artifact_emit_roles"] = ["someone_else"]
        await runtime.run_subagent(
            agent_id="writer",
            task="produce candidate hypotheses",
            work_dir=str(tmp_path / "seat"),
            context=ctx,
        )
        assert len(client.requests) == 2
