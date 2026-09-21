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
from sciteam.session import FileSessionStore, project

from tests.conftest import DATA_ROOT


class _FakeLlmClient:
    def __init__(self) -> None:
        self.usage = LlmUsage()
        self.config = type("Cfg", (), {"default_max_tokens": 8192})()

    async def acomplete(self, *args, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError


def _runtime(*, budget_chars: int = 1000, keep_recent: int | None = None) -> LlmWorkerRuntime:
    assets = PromptAssets(
        prompts_dir=DATA_ROOT / "assets" / "prompts",
        skills_dir=DATA_ROOT / "assets" / "skills",
    )
    runtime = LlmWorkerRuntime(
        client=_FakeLlmClient(), assets=assets, schemas_dir=DATA_ROOT / "schemas"
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
            prompts_dir=DATA_ROOT / "assets" / "prompts",
            skills_dir=DATA_ROOT / "assets" / "skills",
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
        runtime = LlmWorkerRuntime(client=client, assets=assets, schemas_dir=DATA_ROOT / "schemas")
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


class TestSessionKernelIntegration:
    """E4 (docs/SESSION-KERNEL.md): when a composition root attaches a
    `FileSessionStore`, the inner loop must also produce a faithful,
    hash-chained session log — additively, without changing any of the
    provider-visible `messages` behavior covered above."""

    async def test_session_store_none_by_default_is_a_pure_noop(self, monkeypatch, tmp_path):
        """The overwhelming majority of existing callers never pass
        `session_store`; this must remain silently free of any session I/O."""

        def fake_run(name, args, *, context=None):
            return {"ok": True}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        assets = PromptAssets(
            prompts_dir=DATA_ROOT / "assets" / "prompts", skills_dir=DATA_ROOT / "assets" / "skills"
        )

        class _OnceClient(_FakeLlmClient):
            def __init__(self) -> None:
                super().__init__()
                self._calls = 0

            async def acomplete(self, messages, **kwargs):
                self._calls += 1
                if self._calls == 1:
                    tc = {"id": "c1", "function": {"name": "literature_search", "arguments": "{}"}}
                    return type("Resp", (), {"content": "", "tool_calls": [tc]})()
                return type("Resp", (), {"content": "{}", "tool_calls": None})()

        runtime = LlmWorkerRuntime(
            client=_OnceClient(), assets=assets, schemas_dir=DATA_ROOT / "schemas"
        )
        assert runtime._session_store is None
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        await runtime._complete_with_tools(
            messages,
            tool_definitions=[{"type": "function", "function": {"name": "literature_search"}}],
            tool_context={"mission_workspace": str(tmp_path)},
            tool_trace=[],
            inner_loop=[],
            max_tool_rounds=3,
        )
        assert not (tmp_path / "sessions").exists()

    async def test_attached_session_store_records_a_faithful_chained_log(
        self, monkeypatch, tmp_path
    ):
        def fake_run(name, args, *, context=None):
            return {"answer": 42}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        assets = PromptAssets(
            prompts_dir=DATA_ROOT / "assets" / "prompts", skills_dir=DATA_ROOT / "assets" / "skills"
        )

        class _OnceClient(_FakeLlmClient):
            def __init__(self) -> None:
                super().__init__()
                self._calls = 0

            async def acomplete(self, messages, **kwargs):
                self._calls += 1
                if self._calls == 1:
                    tc = {"id": "c1", "function": {"name": "literature_search", "arguments": "{}"}}
                    return type("Resp", (), {"content": "", "tool_calls": [tc]})()
                return type("Resp", (), {"content": '{"artifact": {}}', "tool_calls": None})()

        session_store = FileSessionStore(tmp_path)
        runtime = LlmWorkerRuntime(
            client=_OnceClient(),
            assets=assets,
            schemas_dir=DATA_ROOT / "schemas",
            session_store=session_store,
        )
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        tool_context = {
            "mission_workspace": str(tmp_path),
            "team_run_id": "run1",
            "agent_key": "implementer",
            "work_item_id": "w1",
        }
        await runtime._complete_with_tools(
            messages,
            tool_definitions=[{"type": "function", "function": {"name": "literature_search"}}],
            tool_context=tool_context,
            tool_trace=[],
            inner_loop=[],
            max_tool_rounds=3,
        )

        session_id = runtime._session_id_for(tool_context)
        events = list(session_store.read_from(session_id))
        kinds = [e.kind for e in events]
        assert "user_message" in kinds
        assert "tool_call" in kinds
        assert "tool_result" in kinds
        assert "assistant_message" in kinds
        assert session_store.verify_chain(session_id).ok

        # tool_result must carry a content-addressed ref the archive actually
        # has (docs/SESSION-KERNEL.md §5.1), not a bare filesystem path.
        result_event = next(e for e in events if e.kind == "tool_result")
        archived = session_store.read_archived(result_event.payload["result_ref"])
        assert '"answer": 42' in archived

        # projection is deterministic and pairs the call with its result
        ctx = project(events)
        tool_msgs = [m for m in ctx.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "c1"
        assert "interrupted" not in tool_msgs[0]

    async def test_session_logging_failure_never_breaks_the_inner_loop(self, monkeypatch, tmp_path):
        """A broken session store must degrade to a warning, not a crash —
        same discipline as the existing `except OSError` around inner_loop
        trace persistence."""

        def fake_run(name, args, *, context=None):
            return {"ok": True}

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        assets = PromptAssets(
            prompts_dir=DATA_ROOT / "assets" / "prompts", skills_dir=DATA_ROOT / "assets" / "skills"
        )

        class _BrokenStore:
            def append(self, *a, **kw):
                raise OSError("disk is on fire")

        class _OnceClient(_FakeLlmClient):
            async def acomplete(self, messages, **kwargs):
                return type("Resp", (), {"content": "{}", "tool_calls": None})()

        runtime = LlmWorkerRuntime(
            client=_OnceClient(),
            assets=assets,
            schemas_dir=DATA_ROOT / "schemas",
            session_store=_BrokenStore(),
        )
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        resp = await runtime._complete_with_tools(
            messages,
            tool_definitions=[],
            tool_context={"mission_workspace": str(tmp_path)},
            tool_trace=[],
            inner_loop=[],
            max_tool_rounds=3,
        )
        assert resp.content == "{}"


class TestArchiveAlways:
    async def test_oversize_tool_result_is_fully_archived(self, monkeypatch, tmp_path):
        huge = {"blob": "z" * 30_000}

        def fake_run(name, args, *, context=None):
            return huge

        monkeypatch.setattr("sciteam.wizard_tools.run_research_tool", fake_run)
        monkeypatch.setattr("sciteam.wizard_tools.tool_permission", lambda name: "readonly")
        assets = PromptAssets(
            prompts_dir=DATA_ROOT / "assets" / "prompts",
            skills_dir=DATA_ROOT / "assets" / "skills",
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
            client=_OnceClient(), assets=assets, schemas_dir=DATA_ROOT / "schemas"
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
