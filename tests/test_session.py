"""Contract tests for sciteam/session.py (docs/SESSION-KERNEL.md)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sciteam.session import (
    FileSessionStore,
    SessionError,
    entry_hash,
    project,
)


@pytest.fixture
def store(tmp_path: Path) -> FileSessionStore:
    return FileSessionStore(tmp_path)


def test_append_and_read_roundtrip(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "hello"})
    store.append("s1", "assistant_message", {"content": "hi"})
    events = list(store.read_from("s1"))
    assert [e.kind for e in events] == ["user_message", "assistant_message"]
    assert events[0].seq == 1
    assert events[1].prev_hash == events[0].hash


def test_unknown_kind_rejected(store: FileSessionStore):
    with pytest.raises(SessionError):
        store.append("s1", "not_a_real_kind", {})


def test_verify_chain_ok_on_clean_log(store: FileSessionStore):
    for i in range(5):
        store.append("s1", "user_message", {"content": str(i)})
    verdict = store.verify_chain("s1")
    assert verdict.ok
    assert verdict.length == 5
    assert not verdict.problems


def test_verify_chain_detects_tampering(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "a"})
    store.append("s1", "user_message", {"content": "b"})
    path = store._path("s1")
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["content"] = "TAMPERED"
    # keep the original (now-wrong) hash to simulate an edit that did not
    # also recompute it — the realistic attack event_log.py's docstring
    # calls out.
    lines[0] = json.dumps(tampered, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fresh_store = FileSessionStore(path.parent.parent)
    verdict = fresh_store.verify_chain("s1")
    assert not verdict.ok
    assert any("hash mismatch" in p for p in verdict.problems)


def test_resume_continues_same_chain_not_a_new_one(tmp_path: Path):
    store1 = FileSessionStore(tmp_path)
    store1.append("s1", "user_message", {"content": "first"})
    # Simulate a fresh process reopening the same workspace.
    store2 = FileSessionStore(tmp_path)
    second = store2.append("s1", "user_message", {"content": "second"})
    assert second.seq == 2
    first = next(iter(store2.read_from("s1")))
    assert second.prev_hash == first.hash
    assert store2.verify_chain("s1").ok


def test_torn_last_line_is_truncated_not_fatal(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    store.append("s1", "user_message", {"content": "ok"})
    path = store._path("s1")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "session_id": "s1", "kind": "user_mess')  # torn
    fresh = FileSessionStore(tmp_path)
    events = list(fresh.read_from("s1"))
    assert len(events) == 1  # torn line dropped, not raised
    # Appending after resume must extend the surviving chain cleanly.
    appended = fresh.append("s1", "user_message", {"content": "recovered"})
    assert appended.seq == 2
    assert fresh.verify_chain("s1").ok


_KILL_DRIVER = """
import sys, time
from pathlib import Path
from sciteam.session import FileSessionStore

workspace = Path(sys.argv[1])
store = FileSessionStore(workspace)
for i in range(3):
    store.append("s1", "user_message", {"content": f"turn {i}"})
(workspace / "ready").write_text("now")
time.sleep(300)
"""


def test_real_sigkill_mid_session_then_resume_extends_same_chain(tmp_path: Path):
    """`try/finally` can paper over a simulated crash; this sends a real
    SIGKILL so nothing in the child gets a chance to tidy up, mirroring
    `tests/test_checkpoint.py::TestRealProcessKill` for the institution
    ledger — applied here to the session log's own crash-safety claim."""
    import signal

    driver = tmp_path / "driver.py"
    driver.write_text(_KILL_DRIVER, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    proc = subprocess.Popen([sys.executable, str(driver), str(workspace)])
    ready = workspace / "ready"
    deadline = time.time() + 10
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "child never reached its checkpoint"
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)

    resumed = FileSessionStore(workspace)
    pre_crash = list(resumed.read_from("s1"))
    assert [e.payload["content"] for e in pre_crash] == ["turn 0", "turn 1", "turn 2"]
    assert resumed.verify_chain("s1").ok

    appended = resumed.append("s1", "user_message", {"content": "turn 3 after resume"})
    assert appended.seq == 4
    assert appended.prev_hash == pre_crash[-1].hash
    assert resumed.verify_chain("s1").ok


def test_fork_copies_prefix_and_leaves_source_untouched(store: FileSessionStore):
    for i in range(4):
        store.append("s1", "user_message", {"content": str(i)})
    store.fork("s1", at_seq=2, new_session_id="s1-fork")
    forked = list(store.read_from("s1-fork"))
    assert [e.payload["content"] for e in forked] == ["0", "1"]
    assert store.verify_chain("s1-fork").ok
    # source is unaffected
    source = list(store.read_from("s1"))
    assert len(source) == 4


def test_fork_rejects_unknown_seq(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "0"})
    with pytest.raises(SessionError):
        store.fork("s1", at_seq=99, new_session_id="s1-fork")


def test_fork_rejects_same_session_id(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "0"})
    with pytest.raises(SessionError):
        store.fork("s1", at_seq=1, new_session_id="s1")


def test_archive_and_read_back_content_addressed(store: FileSessionStore):
    ref = store.archive("a very long tool result " * 100)
    assert store.read_archived(ref).startswith("a very long tool result")
    with pytest.raises(SessionError):
        store.read_archived("0" * 64)


def test_entry_hash_excludes_hash_field_itself():
    record = {"seq": 1, "hash": "whatever"}
    without = {"seq": 1}
    assert entry_hash(record) == entry_hash(without)


# --------------------------------------------------------------- projection


def test_projection_pins_are_unconditional_prefix():
    ctx = project([], pins=["CONSTITUTION TEXT", "seat authority: may_veto_exit"])
    assert ctx.messages[0]["pinned"] is True
    assert "CONSTITUTION TEXT" in ctx.messages[0]["content"]


def test_projection_is_deterministic_given_same_events(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "hi"})
    store.append(
        "s1",
        "assistant_message",
        {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]},
    )
    store.append("s1", "tool_result", {"call_id": "c1", "result_ref": "abc", "truncated": False})
    events = list(store.read_from("s1"))
    ctx1 = project(events, pins=["PIN"])
    ctx2 = project(events, pins=["PIN"])
    assert ctx1 == ctx2


def test_projection_pairs_tool_call_and_result():
    store_events = []

    class _Fake:
        pass

    # Build events manually via a real store to keep hash-chain realistic.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        s = FileSessionStore(Path(tmp))
        s.append("s1", "user_message", {"content": "do it"})
        s.append(
            "s1",
            "assistant_message",
            {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]},
        )
        s.append("s1", "tool_result", {"call_id": "c1", "result_ref": "ref1", "truncated": False})
        store_events = list(s.read_from("s1"))
    ctx = project(store_events)
    tool_msgs = [m for m in ctx.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert "interrupted" not in tool_msgs[0]


def test_projection_marks_crash_between_call_and_result_as_interrupted(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "do it"})
    store.append(
        "s1",
        "assistant_message",
        {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]},
    )
    # Simulate: no tool_result was ever written (crash), but a *later* turn
    # happened anyway (e.g. after resume the model was re-prompted) — this
    # is the "not the most recent event" branch, must render interrupted.
    store.append("s1", "user_message", {"content": "are you still there?"})
    events = list(store.read_from("s1"))
    ctx = project(events)
    tool_msgs = [m for m in ctx.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["interrupted"] is True


def test_projection_leaves_truly_in_flight_call_pending(store: FileSessionStore):
    store.append("s1", "user_message", {"content": "do it"})
    store.append(
        "s1",
        "assistant_message",
        {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]},
    )
    events = list(store.read_from("s1"))
    ctx = project(events)
    # The most recent event is the call itself — no synthetic "interrupted"
    # tool message should be fabricated for it.
    assert not any(m["role"] == "tool" for m in ctx.messages)


def test_projection_hides_events_before_latest_compaction():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        s = FileSessionStore(Path(tmp))
        s.append("s1", "user_message", {"content": "old turn 1"})
        s.append("s1", "user_message", {"content": "old turn 2"})
        s.append(
            "s1",
            "compaction",
            {
                "trigger": "pressure",
                "from_seq": 1,
                "to_seq": 2,
                "summary": "user asked two things",
                "summary_ref": "deadbeef",
                "tokens_before": 500,
                "tokens_after": 50,
                "pins_preserved": True,
            },
        )
        s.append("s1", "user_message", {"content": "new turn after compaction"})
        events = list(s.read_from("s1"))
    ctx = project(events)
    contents = [m.get("content") for m in ctx.messages]
    assert "old turn 1" not in contents
    assert "old turn 2" not in contents
    assert "new turn after compaction" in contents
    assert any("user asked two things" in str(c) for c in contents)
