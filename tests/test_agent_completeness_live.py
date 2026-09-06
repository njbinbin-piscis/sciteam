"""Minimal live substrate acceptance across two declarative paradigms."""

from __future__ import annotations

import pytest
from sciteam.mission import MissionBudget, MissionSpec

from tests.conftest import ArtifactWritingRuntime


def _artifact() -> dict:
    return {
        "problem_id": "generic",
        "candidates": [
            {
                "hypothesis_id": "h1",
                "statement": (
                    "A declared mechanism changes the observable outcome under intervention."
                ),
                "falsification_criterion": "The frozen observation does not change.",
                "rationale": "A minimal fixture checks orchestration rather than domain content.",
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("paradigm", ["co_scientist_tournament", "pipeline_chain"])
async def test_two_paradigms_complete_without_engine_special_cases(
    paradigm: str, make_runner
) -> None:
    mission_id = f"live_{paradigm}"
    runtime = ArtifactWritingRuntime({mission_id: _artifact()})
    runner = make_runner(runtime)
    outcome = await runner.run(
        MissionSpec(
            id=mission_id,
            goal="Exercise the same engine with a different institutional graph.",
            paradigm=paradigm,
            exit_contract="candidate_hypotheses",
            budget=MissionBudget(max_rounds=20),
        )
    )
    assert outcome.succeeded, outcome.contract_errors
    assert runtime.calls
