"""P0-4: `_fit_to_context` assembles the prompt against an actual token
budget (context_window - max_tokens - headroom, conservative chars/token)
instead of independently-fixed per-section clip constants, and degrades in a
deterministic, fixed order — kb_digest, then mission_inputs, then the working
pack, then per-skill text — recording every step it took.
"""

from __future__ import annotations

import json

from sciteam import LlmWorkerRuntime, PromptAssets
from sciteam.compact import build_working_pack, profile_for
from sciteam.llm import LlmResponse, LlmUsage
from sciteam.persist import inner_loop_path

from tests.conftest import LAB_ROOT


class _CtxFakeLlmClient:
    """Lets a test dial in an exact `context_window`/`default_max_tokens`."""

    def __init__(self, *, context_window: int, default_max_tokens: int) -> None:
        self.config = type(
            "Cfg",
            (),
            {"context_window": context_window, "default_max_tokens": default_max_tokens},
        )()
        self.usage = LlmUsage()

    async def acomplete(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


class _EnvelopeFakeLlmClient(_CtxFakeLlmClient):
    """Same configurable `config`, but actually answers `acomplete` so
    `run_subagent` can complete end-to-end."""

    def __init__(self, *, context_window: int, default_max_tokens: int, envelope: str) -> None:
        super().__init__(context_window=context_window, default_max_tokens=default_max_tokens)
        self._envelope = envelope

    async def acomplete(self, messages, *, temperature=0.7, max_tokens=4096, **kwargs):
        del temperature, max_tokens, kwargs
        return LlmResponse(content=self._envelope, usage={"total_tokens": 15})


class _FakeAssets:
    """Duck-typed stand-in for `PromptAssets` so skill-text size is fully
    controlled without depending on real `assets/skills/*` file contents."""

    def __init__(self, *, raw_skill_len: int, max_skill_chars: int = 16000) -> None:
        self.max_skill_chars = max_skill_chars
        self._raw_skill_len = raw_skill_len

    def skill_pack(self, allowlist, *, max_chars_override=None):
        if not allowlist:
            return ""
        cap = self.max_skill_chars if max_chars_override is None else max_chars_override
        return "S" * min(self._raw_skill_len, cap)


def _real_assets() -> PromptAssets:
    return PromptAssets(
        prompts_dir=LAB_ROOT / "assets" / "prompts",
        skills_dir=LAB_ROOT / "assets" / "skills",
    )


def _runtime(client, assets=None, *, max_tokens: int | None = None) -> LlmWorkerRuntime:
    return LlmWorkerRuntime(
        client=client,
        assets=assets or _real_assets(),
        schemas_dir=LAB_ROOT / "schemas",
        max_tokens=max_tokens,
    )


def _budget_client(budget_chars_target: int, *, max_tokens: int = 1000):
    """Reverse the budget formula to build a client that yields a budget of
    (at least) roughly `budget_chars_target`, staying clear of the 20k floor.
    """
    diff = budget_chars_target // 3
    context_window = max_tokens + 2000 + diff
    client = _CtxFakeLlmClient(context_window=context_window, default_max_tokens=max_tokens)
    return client, max(20_000, diff * 3)


class TestProfileScale:
    def test_scale_tightens_profile_with_floor(self):
        base = profile_for("implementer")
        tight = profile_for("implementer", scale=0.5)
        assert tight.keep_last_results < base.keep_last_results
        assert tight.max_chars_per_item < base.max_chars_per_item
        assert tight.max_total_chars < base.max_total_chars
        # never degrades below a usable floor
        floor = profile_for("implementer", scale=0.01)
        assert floor.keep_last_results >= 2
        assert floor.max_chars_per_item >= 1000
        assert floor.max_total_chars >= 8000

    def test_scale_one_is_identity(self):
        assert profile_for("writer", scale=1.0) == profile_for("writer")

    def test_build_working_pack_scale_shrinks_render(self):
        history = [
            {"agent_key": "tester", "round_index": i, "state": "done", "result": "r" * 5000}
            for i in range(20)
        ]
        full = build_working_pack(agent_key="tester", task_history=history, scale=1.0)
        half = build_working_pack(agent_key="tester", task_history=history, scale=0.5)
        assert len(half.render()) < len(full.render())


class TestFitToContext:
    def test_no_degrade_when_within_budget(self):
        runtime = _runtime(_CtxFakeLlmClient(context_window=256_000, default_max_tokens=8192))
        fit = runtime._fit_to_context(
            agent_key="worker",
            pins=["p"],
            history=[],
            seat_memory=[],
            kb_digest={},
            mission_inputs={},
            skills_allowlist=[],
            fixed_overhead_chars=500,
        )
        assert fit["steps"] == []
        assert fit["degraded"] is False
        assert fit["over_budget"] is False

    def test_kb_digest_degrades_first_and_can_resolve_alone(self):
        client, budget = _budget_client(30_000)
        runtime = _runtime(client, max_tokens=1000)
        kb_digest = {"x": "a" * 40_000}
        fit = runtime._fit_to_context(
            agent_key="worker",
            pins=[],
            history=[],
            seat_memory=[],
            kb_digest=kb_digest,
            mission_inputs={},
            skills_allowlist=[],
            fixed_overhead_chars=1000,
        )
        assert fit["budget_chars"] == budget
        assert fit["steps"] == ["kb_digest:50000->25000"]
        assert fit["kb_digest_budget"] == 25_000
        assert fit["estimated_chars"] <= fit["budget_chars"]
        assert fit["over_budget"] is False
        # The actual rendered kb_digest at that budget is capped accordingly.
        raw_len = len(json.dumps(kb_digest, ensure_ascii=False))
        assert raw_len > fit["kb_digest_budget"]  # confirms the cap actually bites

    def test_mission_inputs_degrades_after_kb(self):
        client, budget = _budget_client(75_000)
        runtime = _runtime(client, max_tokens=1000)
        mission_inputs = {"x": "a" * 90_000}
        fit = runtime._fit_to_context(
            agent_key="worker",
            pins=[],
            history=[],
            seat_memory=[],
            kb_digest={},
            mission_inputs=mission_inputs,
            skills_allowlist=[],
            fixed_overhead_chars=1000,
        )
        assert fit["steps"] == ["mission_inputs:200000->60000"]
        assert fit["mission_inputs_budget"] == 60_000
        assert fit["estimated_chars"] <= fit["budget_chars"]

    def test_working_pack_scale_degrades_when_pack_is_the_bulk(self):
        history = [
            {"agent_key": "tester", "round_index": i, "state": "done", "result": "r" * 5000}
            for i in range(20)
        ]
        full_len = len(
            build_working_pack(agent_key="tester", task_history=history, scale=1.0).render()
        )
        half_len = len(
            build_working_pack(agent_key="tester", task_history=history, scale=0.5).render()
        )
        assert half_len < full_len
        target = half_len + (full_len - half_len) // 2
        client, budget = _budget_client(target, max_tokens=1000)
        assert half_len < budget < full_len, "test budget must land strictly between the two sizes"
        runtime = _runtime(client, max_tokens=1000)
        fit = runtime._fit_to_context(
            agent_key="tester",
            pins=[],
            history=history,
            seat_memory=[],
            kb_digest={},
            mission_inputs={},
            skills_allowlist=[],
            fixed_overhead_chars=0,
        )
        assert "working_pack:scale=0.5" in fit["steps"]
        assert fit["pack_scale"] == 0.5
        assert len(fit["pack_text"]) == half_len
        assert fit["estimated_chars"] <= fit["budget_chars"]

    def test_skills_degrade_last_after_working_pack_step(self):
        assets = _FakeAssets(raw_skill_len=50_000)
        # Floor budget (20_000): tiny context_window forces the 20k floor.
        client = _CtxFakeLlmClient(context_window=1000, default_max_tokens=100)
        runtime = _runtime(client, assets=assets, max_tokens=100)
        fit = runtime._fit_to_context(
            agent_key="worker",
            pins=[],
            history=[],
            seat_memory=[],
            kb_digest={},
            mission_inputs={},
            skills_allowlist=["dummy.skill"],
            fixed_overhead_chars=10_000,
        )
        assert fit["budget_chars"] == 20_000
        assert fit["steps"] == ["working_pack:scale=0.5", "skills:16000->8000/skill"]
        assert fit["skill_max_chars"] == 8000
        assert len(fit["skill_text"]) == 8000
        assert fit["estimated_chars"] <= fit["budget_chars"]

    def test_cascades_through_all_steps_when_nothing_alone_suffices(self):
        """Every reducible section is oversized; all four steps fire, in
        priority order, and the result is strictly smaller than doing
        nothing — even if it still can't fully close the gap."""
        assets = _FakeAssets(raw_skill_len=50_000)
        history = [
            {"agent_key": "tester", "round_index": i, "state": "done", "result": "r" * 5000}
            for i in range(20)
        ]
        client = _CtxFakeLlmClient(context_window=1000, default_max_tokens=100)
        runtime = _runtime(client, assets=assets, max_tokens=100)
        fit = runtime._fit_to_context(
            agent_key="tester",
            pins=[],
            history=history,
            seat_memory=[],
            kb_digest={"x": "a" * 40_000},
            mission_inputs={"y": "b" * 90_000},
            skills_allowlist=["dummy.skill"],
            fixed_overhead_chars=1000,
        )
        assert fit["degraded"] is True
        assert fit["steps"] == [
            "kb_digest:50000->25000",
            "mission_inputs:200000->60000",
            "working_pack:scale=0.5",
            "skills:16000->8000/skill",
            "working_pack:scale=0.25",
        ]
        undedegraded_total = (
            1000 + 40_000 + 90_000 + 50_000 + len(build_working_pack(agent_key="tester", task_history=history).render())
        )
        assert fit["estimated_chars"] < undedegraded_total


class TestContextBudgetTrace:
    async def test_run_subagent_records_context_budget_step(self, tmp_path):
        """The degrade decision must be replayable from the persisted
        inner-loop trace, not just live in memory."""
        envelope = json.dumps({"summary": "ok", "facts": [], "artifact": None})
        client = _EnvelopeFakeLlmClient(
            context_window=13_000, default_max_tokens=1000, envelope=envelope
        )
        runtime = _runtime(client, max_tokens=1000)
        kb_digest = {"x": "a" * 40_000}
        await runtime.run_subagent(
            agent_id="worker",
            task="mission brief",
            work_dir=str(tmp_path),
            context={
                "team_run_id": "run1",
                "team_agent_key": "worker",
                "work_item_id": "wi1",
                "team_metadata": {
                    "mission_id": "m1",
                    "exit_contract": "candidate_hypotheses",
                    "skills_allowlist": [],
                    "artifact_path": str(tmp_path / "artifact.json"),
                    "artifact_emit_roles": [],
                    "kb_digest": kb_digest,
                },
                "round_index": 1,
            },
        )
        trace_path = inner_loop_path(tmp_path, "wi1")
        assert trace_path.is_file()
        rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        budget_rows = [r for r in rows if r.get("kind") == "context_budget"]
        assert budget_rows, "expected a context_budget step in the persisted inner-loop trace"
        row = budget_rows[0]
        assert row["degraded"] is True
        # Real prompt scaffolding (constitution/role/schema) adds fixed
        # overhead beyond this test's isolated unit cases, so more than the
        # kb_digest step may fire — assert it fired, and fired first.
        assert row["steps"][0] == "kb_digest:50000->25000"
        assert row["budget_chars"] > 0
