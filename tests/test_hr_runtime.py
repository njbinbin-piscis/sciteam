"""Observable HR officer staffing sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sciteam.hr_runtime import (
    list_hr_sessions,
    read_hr_session,
    run_hr_staffing_session,
)
from sciteam.llm import LlmResponse


@dataclass
class ScriptedHrClient:
    """Minimal LLM stub that drives one tool round then a JSON envelope."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    _step: int = 0

    async def acomplete(self, messages, **kwargs) -> LlmResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self._step += 1
        if self._step == 1:
            return LlmResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "list_role_templates",
                            "arguments": "{}",
                        },
                    },
                    {
                        "id": "tc2",
                        "type": "function",
                        "function": {
                            "name": "recruit_agent",
                            "arguments": json.dumps(
                                {
                                    "agent_key": "literature_lead",
                                    "template_id": "literature_lead",
                                    "train_notes": "Scout literature for this mission.",
                                }
                            ),
                        },
                    },
                    {
                        "id": "tc3",
                        "type": "function",
                        "function": {
                            "name": "recruit_agent",
                            "arguments": json.dumps(
                                {
                                    "agent_key": "evaluator",
                                    "template_id": "evaluator",
                                }
                            ),
                        },
                    },
                ],
            )
        return LlmResponse(
            content=json.dumps(
                {
                    "summary": "staffed literature_lead + evaluator",
                    "roster": [
                        {"agent_key": "literature_lead", "template_id": "literature_lead"},
                        {"agent_key": "evaluator", "template_id": "evaluator"},
                    ],
                    "rationale": "Matched seats to catalog templates via HR tools.",
                }
            )
        )


@pytest.mark.asyncio
async def test_deterministic_hr_session_is_observable(tmp_path: Path):
    run_dir = tmp_path / "camp_hr_det"
    run_dir.mkdir()
    result = await run_hr_staffing_session(
        run_dir,
        [
            {"agent_key": "literature_lead", "name": "Lit", "role": "literature_lead"},
            {"agent_key": "evaluator", "name": "Eval", "role": "evaluator"},
        ],
        mission_id="m_scout",
        mission_goal="find gaps",
        client=None,
    )
    assert result.mode == "deterministic_fallback"
    assert {a["agent_key"] for a in result.roster} == {"literature_lead", "evaluator"}
    sessions = list_hr_sessions(run_dir)
    assert len(sessions) == 1
    detail = read_hr_session(run_dir, sessions[0]["session_id"])
    assert detail["report"]["mode"] == "deterministic_fallback"
    assert detail["transcript"]
    assert (run_dir / "pack" / "hr_log.jsonl").is_file()


@pytest.mark.asyncio
async def test_llm_hr_session_records_tool_trace(tmp_path: Path):
    run_dir = tmp_path / "camp_hr_llm"
    run_dir.mkdir()
    client = ScriptedHrClient()
    result = await run_hr_staffing_session(
        run_dir,
        [
            {"agent_key": "literature_lead", "name": "Lit", "role": "literature_lead"},
            {"agent_key": "evaluator", "name": "Eval", "role": "evaluator"},
        ],
        mission_id="m_frame",
        mission_goal="frame the problem",
        client=client,
    )
    assert result.mode == "llm_hr"
    assert len(result.tool_trace) >= 2
    assert "Matched seats" in result.rationale or "literature" in result.rationale.lower()
    sessions = list_hr_sessions(run_dir)
    assert sessions
    detail = read_hr_session(run_dir, sessions[0]["session_id"])
    tools = {t["tool"] for t in detail["tool_trace"]}
    assert "list_role_templates" in tools
    assert "recruit_agent" in tools
    assert detail["report"]["mode"] == "llm_hr"
    assert (run_dir / "pack" / "agents" / "hr_officer" / "AGENT.md").is_file()
