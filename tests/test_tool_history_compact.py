"""P1-6: once a work item's accumulated `role: "tool"` message content in the
same inner loop exceeds `_tool_history_budget_chars`, older tool results are
folded into a one-line digest and their full JSON is persisted to
`<mission_workspace>/tool_history/*.json`; the most recent
`_TOOL_HISTORY_KEEP_RECENT` tool messages are left untouched.
"""

from __future__ import annotations

import json

from sciteam import LlmWorkerRuntime, PromptAssets
from sciteam.llm import LlmUsage

from tests.conftest import LAB_ROOT


class _FakeLlmClient:
    def __init__(self) -> None:
        self.usage = LlmUsage()
        self.config = type("Cfg", (), {"default_max_tokens": 8192})()

    async def acomplete(self, *args, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError


def _runtime(*, budget_chars: int = 1000, keep_recent: int | None = None) -> LlmWorkerRuntime:
    assets = PromptAssets(
        prompts_dir=LAB_ROOT / "assets" / "prompts",
        skills_dir=LAB_ROOT / "assets" / "skills",
    )
    runtime = LlmWorkerRuntime(
        client=_FakeLlmClient(), assets=assets, schemas_dir=LAB_ROOT / "schemas"
    )
    runtime._tool_history_budget_chars = budget_chars
    return runtime


def _tool_msg(call_id: str, chars: int) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "x" * chars}


class TestCompactionThreshold:
    def test_noop_when_under_budget(self, tmp_path):
        runtime = _runtime(budget_chars=10_000)
        messages = [
            {"role": "system", "content": "sys"},
            _tool_msg("c0", 200),
            _tool_msg("c1", 200),
        ]
        inner_loop: list[dict] = []
        runtime._compact_tool_history(
            messages,
            tool_context={"mission_workspace": str(tmp_path)},
            inner_loop=inner_loop,
            tool_round=0,
        )
        assert messages[1]["content"] == "x" * 200
        assert messages[2]["content"] == "x" * 200
        assert inner_loop == []
        assert not (tmp_path / "tool_history").exists()

    def test_folds_older_keeps_recent(self, tmp_path):
        runtime = _runtime(budget_chars=1000)
        # 6 tool messages of 600 chars each -> well over budget; only the
        # last 3 (_TOOL_HISTORY_KEEP_RECENT) should survive verbatim.
        messages = [{"role": "system", "content": "sys"}] + [
            _tool_msg(f"c{i}", 600) for i in range(6)
        ]
        inner_loop: list[dict] = []
        runtime._compact_tool_history(
            messages,
            tool_context={"mission_workspace": str(tmp_path)},
            inner_loop=inner_loop,
            tool_round=2,
        )
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        compacted = [m for m in tool_msgs if str(m["content"]).startswith("[compacted]")]
        untouched = [m for m in tool_msgs if not str(m["content"]).startswith("[compacted]")]
        assert len(compacted) == 3
        assert len(untouched) == 3
        assert all(m["content"] == "x" * 600 for m in untouched)
        # The 3 most recent (last in the list) must be the untouched ones.
        assert tool_msgs[-3:] == untouched

        history_dir = tmp_path / "tool_history"
        assert history_dir.is_dir()
        written = list(history_dir.glob("*.json"))
        assert len(written) == 3
        for path in written:
            assert path.read_text(encoding="utf-8") == "x" * 600

        assert len(inner_loop) == 1
        row = inner_loop[0]
        assert row["kind"] == "tool_history_compact"
        assert row["messages_compacted"] == 3
        assert row["chars_saved"] > 0
        assert row["tool_round"] == 2

    def test_full_content_recoverable_from_disk_pointer(self, tmp_path):
        """The digest must actually name a real, readable file — not just claim to."""
        runtime = _runtime(budget_chars=100)
        original = json.dumps({"papers": list(range(200))})
        messages = [
            _tool_msg("keep0", len(original)),
            _tool_msg("keep1", len(original)),
            _tool_msg("keep2", len(original)),
            {"role": "tool", "tool_call_id": "old", "content": original},
        ]
        # Put the "old" one first so it is the only compactable entry once
        # the last 3 are kept recent.
        messages = [messages[3], messages[0], messages[1], messages[2]]
        inner_loop: list[dict] = []
        runtime._compact_tool_history(
            messages,
            tool_context={"mission_workspace": str(tmp_path)},
            inner_loop=inner_loop,
            tool_round=0,
        )
        digest = messages[0]["content"]
        assert digest.startswith("[compacted]")
        path_str = digest.split("audit copy: ", 1)[1].split(")", 1)[0]
        from pathlib import Path

        assert Path(path_str).read_text(encoding="utf-8") == original

    def test_already_compacted_message_is_not_recompacted(self, tmp_path):
        runtime = _runtime(budget_chars=100)
        messages = [
            {"role": "tool", "tool_call_id": "old", "content": "[compacted] already done"},
            _tool_msg("c1", 200),
            _tool_msg("c2", 200),
            _tool_msg("c3", 200),
        ]
        inner_loop: list[dict] = []
        runtime._compact_tool_history(
            messages,
            tool_context={"mission_workspace": str(tmp_path)},
            inner_loop=inner_loop,
            tool_round=0,
        )
        # Only the already-compacted message would have been eligible (the
        # other 3 are "recent"); since it is already folded, nothing changes.
        assert messages[0]["content"] == "[compacted] already done"
        assert inner_loop == []

    def test_short_tool_messages_are_not_worth_compacting(self, tmp_path):
        """Below the 500-char disk round-trip floor, leave content alone even
        if the *count* of tool messages pushes the total over budget."""
        runtime = _runtime(budget_chars=100)
        messages = [_tool_msg(f"c{i}", 50) for i in range(10)]
        inner_loop: list[dict] = []
        runtime._compact_tool_history(
            messages,
            tool_context={"mission_workspace": str(tmp_path)},
            inner_loop=inner_loop,
            tool_round=0,
        )
        assert all(m["content"] == "x" * 50 for m in messages)
        assert inner_loop == []


class TestIntegrationWithToolLoop:
    async def test_complete_with_tools_bounds_prompt_growth_across_rounds(
        self, monkeypatch, tmp_path
    ):
        """A many-round tool loop with large results must not let the raw
        tool-message total grow unboundedly — microcompaction should kick in
        well before the loop ends."""
        big_result = {"text": "y" * 3000}

        def fake_run(name, args, *, context=None):
            return big_result

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")

        assets = PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
        )

        class _RoundLimitedClient(_FakeLlmClient):
            def __init__(self, n_rounds: int) -> None:
                super().__init__()
                self._n_rounds = n_rounds
                self._calls = 0

            async def acomplete(self, messages, **kwargs):
                self._calls += 1
                if self._calls <= self._n_rounds:
                    tc = {
                        "id": f"c{self._calls}",
                        "function": {"name": "literature_search", "arguments": "{}"},
                    }
                    return type("Resp", (), {"content": "", "tool_calls": [tc]})()
                return type("Resp", (), {"content": "{}", "tool_calls": None})()

        client = _RoundLimitedClient(n_rounds=8)
        runtime = LlmWorkerRuntime(client=client, assets=assets, schemas_dir=LAB_ROOT / "schemas")
        runtime._tool_history_budget_chars = 5000

        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        tool_trace: list[dict] = []
        inner_loop: list[dict] = []
        await runtime._complete_with_tools(
            messages,
            tool_definitions=[{"type": "function", "function": {"name": "literature_search"}}],
            tool_context={"mission_workspace": str(tmp_path)},
            tool_trace=tool_trace,
            inner_loop=inner_loop,
            max_tool_rounds=10,
        )

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 8
        live_chars = sum(
            len(m["content"]) for m in tool_msgs if not str(m["content"]).startswith("[compacted]")
        )
        # With an ~5000-char budget and 3000-char results, compaction must
        # have fired at least once; the live (uncompacted) total stays
        # bounded instead of growing to 8*3000=24000.
        assert live_chars < 3000 * 8
        compact_events = [row for row in inner_loop if row.get("kind") == "tool_history_compact"]
        assert compact_events, "expected at least one tool_history_compact trace row"
        assert (tmp_path / "tool_history").is_dir()
        archived = list((tmp_path / "tool_history").glob("*.json"))
        assert archived, "every tool result must be archived, not only compacted ones"
        assert any(len(p.read_text(encoding="utf-8")) >= 3000 for p in archived)


class TestArchiveAlways:
    async def test_oversize_tool_result_is_fully_archived(self, monkeypatch, tmp_path):
        huge = {"blob": "z" * 30_000}

        def fake_run(name, args, *, context=None):
            return huge

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        assets = PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
        )

        class _OnceClient(_FakeLlmClient):
            def __init__(self) -> None:
                super().__init__()
                self._calls = 0

            async def acomplete(self, messages, **kwargs):
                self._calls += 1
                if self._calls == 1:
                    return type(
                        "Resp",
                        (),
                        {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "big1",
                                    "function": {"name": "literature_search", "arguments": "{}"},
                                }
                            ],
                        },
                    )()
                return type("Resp", (), {"content": "{}", "tool_calls": None})()

        runtime = LlmWorkerRuntime(
            client=_OnceClient(), assets=assets, schemas_dir=LAB_ROOT / "schemas"
        )
        runtime._tool_result_visible_chars = 8_000
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        inner_loop: list[dict] = []
        await runtime._complete_with_tools(
            messages,
            tool_definitions=[{"type": "function", "function": {"name": "literature_search"}}],
            tool_context={"mission_workspace": str(tmp_path)},
            tool_trace=[],
            inner_loop=inner_loop,
            max_tool_rounds=3,
        )
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs
        assert "audit copy:" in tool_msgs[0]["content"]
        assert len(tool_msgs[0]["content"]) < 30_000
        archive = tmp_path / "tool_history"
        files = list(archive.glob("*.json"))
        assert files
        assert any("z" * 30_000 in p.read_text(encoding="utf-8") for p in files)
