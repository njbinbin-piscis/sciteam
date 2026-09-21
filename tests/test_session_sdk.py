"""docs/SESSION-KERNEL.md §8 — the embeddable Session SDK."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.compaction_port import MechanicalCompactionPort
from sciteam.session_sdk import Session, SessionRuntimeError, ToolRuntime
from sciteam.token_meter import SimpleTokenMeter


class _Resp:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _EchoProvider:
    """No tools: always answers immediately."""

    async def acomplete(self, messages, *, tools=None, tool_choice=None):
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return _Resp(content=f"echo: {last_user}")


class _OneToolThenAnswerProvider:
    def __init__(self):
        self._calls = 0

    async def acomplete(self, messages, *, tools=None, tool_choice=None):
        self._calls += 1
        if self._calls == 1:
            tc = {"id": "c1", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}
            return _Resp(content=None, tool_calls=[tc])
        return _Resp(content="the answer is 5")


class _NeverAnswersProvider:
    async def acomplete(self, messages, *, tools=None, tool_choice=None):
        tc = {"id": "loop", "function": {"name": "noop", "arguments": "{}"}}
        return _Resp(content=None, tool_calls=[tc])


async def test_create_run_and_read_events(tmp_path: Path):
    session = Session.create(workspace=tmp_path, seat="tester", provider=_EchoProvider())
    turn = await session.run("hello there")
    assert turn.content == "echo: hello there"
    kinds = [e.kind for e in session.events()]
    assert kinds == ["user_message", "assistant_message"]


async def test_run_with_tool_call_and_result(tmp_path: Path):
    tools = ToolRuntime(
        definitions=({"type": "function", "function": {"name": "add"}},),
        handlers={"add": lambda args: {"ok": True, "sum": args["a"] + args["b"]}},
    )
    session = Session.create(workspace=tmp_path, provider=_OneToolThenAnswerProvider())
    turn = await session.run("what is 2+3?", tools=tools)
    assert turn.content == "the answer is 5"
    events = list(session.events())
    kinds = [e.kind for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds
    result_event = next(e for e in events if e.kind == "tool_result")
    archived = session._store.read_archived(result_event.payload["result_ref"])
    assert '"sum": 5' in archived


async def test_no_provider_raises():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        session = Session.create(workspace=Path(tmp))
        with pytest.raises(SessionRuntimeError):
            await session.run("hi")


async def test_exhausted_tool_rounds_raises_not_infinite_loop(tmp_path: Path):
    tools = ToolRuntime(handlers={"noop": lambda args: {"ok": True}})
    session = Session.create(workspace=tmp_path, provider=_NeverAnswersProvider())
    with pytest.raises(SessionRuntimeError):
        await session.run("go", tools=tools, max_tool_rounds=2)


async def test_resume_continues_same_session_and_sees_prior_turns(tmp_path: Path):
    s1 = Session.create(workspace=tmp_path, session_id="fixed-id", provider=_EchoProvider())
    await s1.run("first")
    s1.close()

    s2 = Session.resume(workspace=tmp_path, session_id="fixed-id", provider=_EchoProvider())
    await s2.run("second")
    events = list(s2.events())
    contents = [e.payload.get("content") for e in events if e.kind == "user_message"]
    assert contents == ["first", "second"]


def test_resume_rejects_tampered_chain(tmp_path: Path):
    import json

    s1 = Session.create(workspace=tmp_path, session_id="fixed-id")
    s1._store.append("fixed-id", "user_message", {"content": "a"})
    path = s1._store._path("fixed-id")
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["content"] = "TAMPERED"
    path.write_text(json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(SessionRuntimeError):
        Session.resume(workspace=tmp_path, session_id="fixed-id")


async def test_fork_creates_independent_branch(tmp_path: Path):
    session = Session.create(workspace=tmp_path, session_id="base", provider=_EchoProvider())
    await session.run("turn one")
    await session.run("turn two")
    events = list(session.events())
    fork_point = events[1].seq  # after first user+assistant pair

    branch = session.fork(fork_point)
    assert branch.session_id != session.session_id
    await branch.run("branch turn")
    branch_users = [e.payload["content"] for e in branch.events() if e.kind == "user_message"]
    assert branch_users == ["turn one", "branch turn"]
    # original untouched
    base_users = [e.payload["content"] for e in session.events() if e.kind == "user_message"]
    assert base_users == ["turn one", "turn two"]


async def test_compaction_fires_under_pressure_and_keeps_pins_out_of_summary(tmp_path: Path):
    session = Session.create(
        workspace=tmp_path,
        provider=_EchoProvider(),
        pins=["SECRET-PIN-VALUE"],
        compaction_port=MechanicalCompactionPort(pressure_threshold=0.0),
        token_meter=SimpleTokenMeter(),
        pressure_window=1,  # guarantees should_compact fires immediately
        keep_recent_tokens=1,
    )
    await session.run("a reasonably long opening message " * 5)
    await session.run("a second reasonably long message " * 5)
    kinds = [e.kind for e in session.events()]
    assert "compaction" in kinds
    compaction_event = next(e for e in session.events() if e.kind == "compaction")
    assert "SECRET-PIN-VALUE" not in compaction_event.payload["summary"]
