"""Same-seat failures survive work-item boundaries as bounded memory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam.llm import LlmResponse, LlmUsage
from sciteam.llm_worker import LlmWorkerRuntime, PromptAssets

from tests.conftest import LAB_ROOT


class _Client:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []
        self.usage = LlmUsage()

    async def acomplete(self, messages, **_kwargs):
        self.requests.append(messages)
        return LlmResponse(content=self.responses.pop(0), usage={})


def _context(root: Path, work_item: str, wave: int) -> dict:
    return {
        "team_run_id": "run",
        "team_agent_key": "analyst",
        "work_item_id": work_item,
        "round_index": wave,
        "team_metadata": {
            "mission_id": "memory",
            "exit_contract": "candidate_hypotheses",
            "skills_allowlist": [],
            "artifact_path": str(root / "artifact.json"),
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


@pytest.mark.asyncio
async def test_ordinary_success_also_commits_memory(tmp_path: Path) -> None:
    """P1-5: not just failure/repair — a clean success is worth remembering."""
    good = json.dumps({"summary": "clean run", "facts": [], "artifact": None})
    runtime, _ = _runtime([good])
    work_dir = tmp_path / "seat"
    await runtime.run_subagent(
        agent_id="analyst",
        task="method A",
        work_dir=str(work_dir),
        context=_context(tmp_path, "wave1", 1),
    )
    records = (work_dir / "memory" / "seat_memory.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in records.splitlines()]
    assert any(r.get("outcome") == "success" for r in rows)


@pytest.mark.asyncio
async def test_campaign_memory_carries_role_history_across_missions(tmp_path: Path) -> None:
    """A fresh mission's seat for the same role recalls prior missions'
    repair history via the shared `campaign_memory_path`."""
    campaign_memory = tmp_path / "campaign_memory.jsonl"

    def _ctx(work_item: str) -> dict:
        ctx = _context(tmp_path, work_item, 1)
        ctx["team_metadata"]["campaign_memory_path"] = str(campaign_memory)
        return ctx

    # Mission 1: analyst fails once with a distinctive counterexample.
    bad = "still garbage"
    runtime1, _ = _runtime([bad, bad])
    mission1_work_dir = tmp_path / "mission1" / "analyst"
    result1 = await runtime1.run_subagent(
        agent_id="analyst",
        task="review kvcache eviction",
        work_dir=str(mission1_work_dir),
        context=_ctx("m1_wi"),
    )
    assert result1.outcome.value == "recoverable"
    assert campaign_memory.is_file()

    # Mission 2: a *different* work_dir (fresh mission), same role. Its own
    # seat_memory.jsonl starts empty, but campaign memory recall should
    # surface mission 1's failure via role-scoped lookup.
    good = json.dumps({"summary": "ok", "facts": [], "artifact": None})
    runtime2, client2 = _runtime([good])
    mission2_work_dir = tmp_path / "mission2" / "analyst"
    await runtime2.run_subagent(
        agent_id="analyst",
        task="review kvcache eviction policy again",
        work_dir=str(mission2_work_dir),
        context=_ctx("m2_wi"),
    )
    prompt = "\n".join(str(m.get("content") or "") for m in client2.requests[-1])
    assert "Same-seat memory" in prompt
    assert "campaign_role" in prompt


@pytest.mark.asyncio
async def test_next_wave_prompt_recalls_same_seat_failure(tmp_path: Path) -> None:
    good = json.dumps({"summary": "changed method", "facts": [], "artifact": None})
    client = _Client(["bad envelope one", "bad envelope two", good])
    runtime = LlmWorkerRuntime(
        client=client,
        assets=PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
        ),
        schemas_dir=LAB_ROOT / "schemas",
    )
    work_dir = tmp_path / "seat"
    first = await runtime.run_subagent(
        agent_id="analyst",
        task="method A",
        work_dir=str(work_dir),
        context=_context(tmp_path, "wave1", 1),
    )
    assert first.outcome.value == "recoverable"

    await runtime.run_subagent(
        agent_id="analyst",
        task="new card",
        work_dir=str(work_dir),
        context=_context(tmp_path, "wave2", 2),
    )
    prompt = "\n".join(str(message.get("content") or "") for message in client.requests[-1])
    assert "Same-seat memory" in prompt
    assert "bad envelope two" in prompt
    records = (work_dir / "memory" / "seat_memory.jsonl").read_text(encoding="utf-8")
    assert "invalid_json_envelope" in records
