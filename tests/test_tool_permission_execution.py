"""P0-3: `readonly` tool calls in the same round run concurrently; every other
permission class (`side_effect`, `privileged`, unknown) runs serially in the
model's original order. Results are always returned in original call order.
"""

from __future__ import annotations

import threading
import time

from sciteam import LlmWorkerRuntime, PromptAssets
from sciteam.llm import LlmUsage
from sciteam.tool_registry import ToolRegistry

from tests.conftest import LAB_ROOT


class _FakeLlmClient:
    def __init__(self) -> None:
        self.usage = LlmUsage()
        self.config = type("Cfg", (), {"default_max_tokens": 8192})()

    async def acomplete(self, *args, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError


def _runtime() -> LlmWorkerRuntime:
    assets = PromptAssets(
        prompts_dir=LAB_ROOT / "assets" / "prompts",
        skills_dir=LAB_ROOT / "assets" / "skills",
    )
    return LlmWorkerRuntime(
        client=_FakeLlmClient(), assets=assets, schemas_dir=LAB_ROOT / "schemas"
    )


def _tc(call_id: str, name: str) -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": "{}"},
    }


class TestToolRegistryPermission:
    def test_permission_of_known_tools(self, tmp_path):
        registry = ToolRegistry(LAB_ROOT / "assets" / "tools" / "catalog.yaml")
        assert registry.permission_of("literature_search") == "readonly"
        assert registry.permission_of("url_fetch") == "readonly"
        assert registry.permission_of("run_eval") == "side_effect"
        assert registry.permission_of("compute_sandbox") == "side_effect"
        assert registry.permission_of("submit_job") == "privileged"

    def test_unknown_tool_defaults_to_side_effect(self):
        registry = ToolRegistry(LAB_ROOT / "assets" / "tools" / "catalog.yaml")
        assert registry.permission_of("no_such_tool") == "side_effect"


class TestReadonlyConcurrency:
    async def test_consecutive_readonly_calls_overlap_in_time(self, monkeypatch):
        """Three readonly calls that each sleep 60ms should finish in well
        under 3x60ms if they truly run concurrently."""
        starts: list[float] = []
        lock = threading.Lock()

        def fake_run(name, args, *, context=None):
            with lock:
                starts.append(time.monotonic())
            time.sleep(0.06)
            return {"ok": True, "tool": name}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")

        runtime = _runtime()
        calls = [_tc(f"c{i}", "literature_search") for i in range(3)]
        t0 = time.monotonic()
        results = await runtime._run_tool_calls(calls, tool_context={})
        elapsed = time.monotonic() - t0

        assert len(results) == 3
        assert elapsed < 0.06 * 2.5, f"expected concurrent execution, took {elapsed:.3f}s"
        # All three should have started within a small window of each other.
        assert max(starts) - min(starts) < 0.05

    async def test_readonly_group_bounded_by_semaphore(self, monkeypatch):
        """Concurrency never exceeds `_max_parallel_readonly`."""
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_run(name, args, *, context=None):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {"ok": True}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")

        runtime = _runtime()
        runtime._max_parallel_readonly = 2
        calls = [_tc(f"c{i}", "literature_search") for i in range(6)]
        results = await runtime._run_tool_calls(calls, tool_context={})

        assert len(results) == 6
        assert peak <= 2

    async def test_side_effect_calls_run_strictly_serial_and_in_order(self, monkeypatch):
        order: list[str] = []
        lock = threading.Lock()

        def fake_run(name, args, *, context=None):
            with lock:
                order.append(f"start:{name}")
            time.sleep(0.02)
            with lock:
                order.append(f"end:{name}")
            return {"ok": True}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "side_effect")

        runtime = _runtime()
        calls = [_tc("c0", "run_eval"), _tc("c1", "compute_sandbox"), _tc("c2", "model_certify")]
        results = await runtime._run_tool_calls(calls, tool_context={})

        assert [name for _, name, _, _, _ in results] == [
            "run_eval",
            "compute_sandbox",
            "model_certify",
        ]
        # Strictly serial: each tool's end must precede the next tool's start.
        assert order == [
            "start:run_eval",
            "end:run_eval",
            "start:compute_sandbox",
            "end:compute_sandbox",
            "start:model_certify",
            "end:model_certify",
        ]

    async def test_mixed_groups_preserve_original_order(self, monkeypatch):
        """readonly, readonly, side_effect, readonly → grouped as
        [readonly,readonly] concurrent, [side_effect] serial, [readonly] alone,
        but the returned list matches the original call order."""

        def fake_run(name, args, *, context=None):
            return {"tool": name}

        perms = {
            "search_a": "readonly",
            "search_b": "readonly",
            "write_c": "side_effect",
            "search_d": "readonly",
        }
        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: perms[name])

        runtime = _runtime()
        calls = [
            _tc("c0", "search_a"),
            _tc("c1", "search_b"),
            _tc("c2", "write_c"),
            _tc("c3", "search_d"),
        ]
        results = await runtime._run_tool_calls(calls, tool_context={})
        assert [name for _, name, _, _, _ in results] == [
            "search_a",
            "search_b",
            "write_c",
            "search_d",
        ]

    async def test_single_readonly_call_has_no_semaphore_overhead(self, monkeypatch):
        """A lone readonly call (no grouping benefit) still executes and returns correctly."""

        def fake_run(name, args, *, context=None):
            return {"tool": name, "args": args}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")

        runtime = _runtime()
        results = await runtime._run_tool_calls([_tc("c0", "literature_search")], tool_context={})
        assert len(results) == 1
        _, name, _, result, cache_hit = results[0]
        assert name == "literature_search"
        assert result == {"tool": "literature_search", "args": {}}
        assert cache_hit is False


class TestToolResultDedupCache:
    """P1-7: same-work-item (tool, args) result reuse for catalog
    `deterministic: true` tools only."""

    async def test_repeat_call_on_deterministic_tool_hits_cache(self, monkeypatch):
        calls_made: list[tuple[str, dict]] = []

        def fake_run(name, args, *, context=None):
            calls_made.append((name, dict(args)))
            return {"ok": True, "n": len(calls_made)}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        monkeypatch.setattr("sciteam.wizard_tools.tool_deterministic", lambda name: True)

        runtime = _runtime()
        cache: dict = {}
        calls = [
            _tc("c0", "literature_search"),
            _tc("c1", "literature_search"),  # same tool, same (empty) args
        ]
        results = await runtime._run_tool_calls(calls, tool_context={}, cache=cache)

        assert len(calls_made) == 1, "second identical call must not re-invoke the tool"
        _, _, _, result0, hit0 = results[0]
        _, _, _, result1, hit1 = results[1]
        assert hit0 is False
        assert hit1 is True
        assert result0 == result1 == {"ok": True, "n": 1}

    async def test_different_args_are_not_conflated(self, monkeypatch):
        calls_made: list[dict] = []

        def fake_run(name, args, *, context=None):
            calls_made.append(dict(args))
            return {"ok": True, "echo": args}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "side_effect")
        monkeypatch.setattr("sciteam.wizard_tools.tool_deterministic", lambda name: True)

        runtime = _runtime()
        cache: dict = {}
        calls = [
            {"id": "c0", "function": {"name": "run_eval", "arguments": '{"seed": 1}'}},
            {"id": "c1", "function": {"name": "run_eval", "arguments": '{"seed": 2}'}},
        ]
        results = await runtime._run_tool_calls(calls, tool_context={}, cache=cache)

        assert len(calls_made) == 2
        assert [hit for *_, hit in results] == [False, False]

    async def test_non_deterministic_tool_never_cached(self, monkeypatch):
        calls_made: list[int] = []

        def fake_run(name, args, *, context=None):
            calls_made.append(1)
            return {"ok": True}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        monkeypatch.setattr("sciteam.wizard_tools.tool_deterministic", lambda name: False)

        runtime = _runtime()
        cache: dict = {}
        calls = [_tc("c0", "web_search"), _tc("c1", "web_search")]
        results = await runtime._run_tool_calls(calls, tool_context={}, cache=cache)

        assert len(calls_made) == 2, "non-deterministic tools must always re-invoke"
        assert [hit for *_, hit in results] == [False, False]

    async def test_no_cache_dict_disables_caching_entirely(self, monkeypatch):
        """`cache=None` (default) — e.g. a caller that never opted in — must
        still work correctly, just without reuse."""
        calls_made: list[int] = []

        def fake_run(name, args, *, context=None):
            calls_made.append(1)
            return {"ok": True}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        monkeypatch.setattr("sciteam.wizard_tools.tool_deterministic", lambda name: True)

        runtime = _runtime()
        results = await runtime._run_tool_calls(
            [_tc("c0", "literature_search"), _tc("c1", "literature_search")], tool_context={}
        )
        assert len(calls_made) == 2
        assert [hit for *_, hit in results] == [False, False]

    def test_catalog_deterministic_flags_match_expectations(self):
        from sciteam.tool_registry import ToolRegistry

        registry = ToolRegistry(LAB_ROOT / "assets" / "tools" / "catalog.yaml")
        assert registry.deterministic_of("literature_search") is True
        assert registry.deterministic_of("run_eval") is True
        assert registry.deterministic_of("web_search") is False
        assert registry.deterministic_of("url_fetch") is False
        assert registry.deterministic_of("no_such_tool") is False


# TestCacheThreadSafety (concurrent HttpJsonClient fetch) intentionally
# omitted from this export: it exercises harness/adapters/literature/cache.py,
# which lives in the internal research harness and is out of scope for the
# curated open-source release (see paper/rebuildV2/24-oss-release-plan.md §2).
