"""docs/SESSION-KERNEL.md §5.1 — read_archived_result."""

from __future__ import annotations

from pathlib import Path

from sciteam.context_tools import read_archived_result
from sciteam.session import FileSessionStore
from sciteam.worker_common import FS_ALWAYS_CAPABILITIES, effective_tool_capabilities


def test_recovers_content_by_ref(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    ref = store.archive("the full original tool output")
    result = read_archived_result({"result_ref": ref}, {"session_store": store})
    assert result["ok"] is True
    assert result["content"] == "the full original tool output"


def test_unknown_ref_fails_closed(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    result = read_archived_result({"result_ref": "0" * 64}, {"session_store": store})
    assert result["ok"] is False


def test_missing_result_ref_argument(tmp_path: Path):
    store = FileSessionStore(tmp_path)
    result = read_archived_result({}, {"session_store": store})
    assert result["ok"] is False


def test_no_session_store_attached(tmp_path: Path):
    result = read_archived_result({"result_ref": "abc"}, {})
    assert result["ok"] is False


def test_always_granted_regardless_of_declared_capabilities():
    """Every seat must always be able to recover its own compacted history —
    this cannot be something a skill pack opts into (docs/SESSION-KERNEL.md
    §5.1 + worker_common.FS_ALWAYS_CAPABILITIES rationale)."""
    assert "context.read_archived" in FS_ALWAYS_CAPABILITIES
    effective = effective_tool_capabilities(set())
    assert "context.read_archived" in effective


def test_registered_in_mission_tool_defs_and_dispatchable(tmp_path: Path):
    from sciteam.wizard_tools import MISSION_TOOL_DEFS, run_research_tool, tool_permission

    names = [d["function"]["name"] for d in MISSION_TOOL_DEFS]
    assert "read_archived_result" in names
    assert tool_permission("read_archived_result") == "readonly"

    store = FileSessionStore(tmp_path)
    ref = store.archive("dispatch path content")
    result = run_research_tool(
        "read_archived_result", {"result_ref": ref}, context={"session_store": store}
    )
    assert result["ok"] is True
    assert result["content"] == "dispatch path content"
