"""P1-8: full-allowlist skill injection doesn't scale — once a role's
allowlist grows past `_SKILL_TOPK_THRESHOLD`, only the top-k skills by
deterministic lexical overlap with the task card get their body text
injected. Capability derivation (`_required_tool_capabilities`) always sees
the *full* allowlist — selection only narrows what's shown in the prompt,
never what tools the seat is granted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam.llm import LlmResponse, LlmUsage
from sciteam.llm_worker import (
    _SKILL_TOPK_K,
    _SKILL_TOPK_THRESHOLD,
    LlmWorkerRuntime,
    PromptAssets,
)

from tests.conftest import LAB_ROOT


class _FakeLlmClient:
    def __init__(self) -> None:
        self.usage = LlmUsage()
        self.config = type("Cfg", (), {"default_max_tokens": 8192})()

    async def acomplete(self, *args, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError


def _make_skills(skills_dir: Path, bodies: dict[str, str]) -> None:
    for skill_id, body in bodies.items():
        d = skills_dir / skill_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")


def _runtime(skills_dir: Path) -> LlmWorkerRuntime:
    assets = PromptAssets(prompts_dir=LAB_ROOT / "assets" / "prompts", skills_dir=skills_dir)
    return LlmWorkerRuntime(
        client=_FakeLlmClient(), assets=assets, schemas_dir=LAB_ROOT / "schemas"
    )


class TestSelectTopkSkills:
    def test_below_threshold_returns_full_allowlist_unchanged(self, tmp_path):
        skills_dir = tmp_path / "skills"
        allowlist = [f"skill_{i}" for i in range(_SKILL_TOPK_THRESHOLD)]
        _make_skills(skills_dir, {sid: f"body for {sid}" for sid in allowlist})
        runtime = _runtime(skills_dir)
        selected, applied, scores = runtime._select_topk_skills("do anything", allowlist)
        assert selected == allowlist
        assert applied is False
        assert scores == []

    def test_above_threshold_selects_by_task_overlap(self, tmp_path):
        skills_dir = tmp_path / "skills"
        allowlist = [f"skill_{i}" for i in range(_SKILL_TOPK_THRESHOLD + 4)]
        bodies = {sid: "generic filler text with no special terms" for sid in allowlist}
        # Plant a handful of skills whose body clearly matches the task card.
        bodies["skill_2"] = "kvcache eviction policy tuning guidance and tradeoffs"
        bodies["skill_5"] = "kvcache benchmark harness notes and eviction caveats"
        envelope_task = "Design a kvcache eviction policy and benchmark it."
        runtime = _runtime(skills_dir)
        selected, applied, scores = runtime._select_topk_skills(envelope_task, allowlist)

        assert applied is True
        assert len(selected) <= _SKILL_TOPK_K
        assert "skill_2" in selected
        assert "skill_5" in selected
        # Original allowlist relative order is preserved among survivors.
        assert selected == sorted(selected, key=allowlist.index)
        scored_ids = {sid for sid, _ in scores}
        assert {"skill_2", "skill_5"} <= scored_ids

    def test_no_overlap_falls_back_to_allowlist_prefix_deterministically(self, tmp_path):
        """When nothing matches the task, selection must still be
        deterministic (stable on allowlist order), not scattered."""
        skills_dir = tmp_path / "skills"
        allowlist = [f"skill_{i}" for i in range(_SKILL_TOPK_THRESHOLD + 3)]
        _make_skills(skills_dir, {sid: "unrelated filler content" for sid in allowlist})
        runtime = _runtime(skills_dir)
        selected1, _, _ = runtime._select_topk_skills("completely unrelated task text", allowlist)
        selected2, _, _ = runtime._select_topk_skills("completely unrelated task text", allowlist)
        assert selected1 == selected2
        assert selected1 == allowlist[:_SKILL_TOPK_K]

    def test_missing_skill_file_scores_zero_not_crash(self, tmp_path):
        skills_dir = tmp_path / "skills"
        allowlist = [f"skill_{i}" for i in range(_SKILL_TOPK_THRESHOLD + 2)]
        # Only create files for half of them; the rest are dangling ids.
        _make_skills(
            skills_dir, {sid: "kvcache eviction" for sid in allowlist[: len(allowlist) // 2]}
        )
        runtime = _runtime(skills_dir)
        selected, applied, _scores = runtime._select_topk_skills("kvcache eviction task", allowlist)
        assert applied is True
        assert len(selected) <= _SKILL_TOPK_K


class _Client:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []
        self.usage = LlmUsage()

    async def acomplete(self, messages, **_kwargs):
        self.requests.append(messages)
        return LlmResponse(content=self.responses.pop(0), usage={})


class TestWiredIntoRunSubagent:
    @pytest.mark.asyncio
    async def test_capability_surface_unaffected_by_topk_selection(self, tmp_path):
        """A capability declared only by a skill that top-k would drop from
        the *prompt* must still be granted — capability derivation reads the
        full allowlist, not the injected subset."""
        skills_dir = tmp_path / "skills"
        allowlist = [f"filler_{i}" for i in range(_SKILL_TOPK_THRESHOLD + 2)]
        bodies = {sid: "generic filler with no task overlap at all" for sid in allowlist}
        # This skill declares a capability but its body never overlaps the
        # task card lexically, so top-k would rank it last.
        capability_skill = "lit.citation_guard"
        allowlist.append(capability_skill)
        bodies[capability_skill] = (
            "# Cite Guard\n\n## Requires capabilities\n- literature.search\n\nzzz unrelated zzz"
        )
        _make_skills(skills_dir, bodies)

        client = _Client([json.dumps({"summary": "ok", "facts": [], "artifact": None})])
        runtime = LlmWorkerRuntime(
            client=client,
            assets=PromptAssets(prompts_dir=LAB_ROOT / "assets" / "prompts", skills_dir=skills_dir),
            schemas_dir=LAB_ROOT / "schemas",
        )
        selected, applied, _ = runtime._select_topk_skills(
            "do something about kvcache benchmarking", allowlist
        )
        assert applied is True
        assert capability_skill not in selected, "test setup: skill must be dropped from prompt"

        required = runtime._required_tool_capabilities(allowlist, "m1")
        assert "literature.search" in required, (
            "capability surface must come from the FULL allowlist, "
            "not the top-k prompt-injection subset"
        )

    @pytest.mark.asyncio
    async def test_run_subagent_records_skills_selected_trace(self, tmp_path):
        skills_dir = tmp_path / "skills"
        allowlist = [f"skill_{i}" for i in range(_SKILL_TOPK_THRESHOLD + 3)]
        _make_skills(skills_dir, {sid: "kvcache eviction notes" for sid in allowlist})
        envelope = json.dumps({"summary": "ok", "facts": [], "artifact": None})
        client = _Client([envelope, envelope])
        runtime = LlmWorkerRuntime(
            client=client,
            assets=PromptAssets(prompts_dir=LAB_ROOT / "assets" / "prompts", skills_dir=skills_dir),
            schemas_dir=LAB_ROOT / "schemas",
        )
        from sciteam.persist import inner_loop_path

        work_dir = tmp_path / "seat"
        await runtime.run_subagent(
            agent_id="analyst",
            task="kvcache eviction benchmarking task",
            work_dir=str(work_dir),
            context={
                "team_run_id": "run",
                "team_agent_key": "analyst",
                "work_item_id": "wi1",
                "round_index": 1,
                "team_metadata": {
                    "mission_id": "m1",
                    "exit_contract": "candidate_hypotheses",
                    "skills_allowlist": allowlist,
                    "artifact_path": str(tmp_path / "artifact.json"),
                },
            },
        )
        trace_path = inner_loop_path(str(work_dir), "wi1")
        rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        skills_rows = [r for r in rows if r.get("kind") == "skills_selected"]
        assert len(skills_rows) == 1
        row = skills_rows[0]
        assert row["allowlist_size"] == len(allowlist)
        assert row["topk_applied"] is True
        assert len(row["injected"]) <= _SKILL_TOPK_K
