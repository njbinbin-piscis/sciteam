"""Board state machine: waiting_deps vs hard blocked."""

from __future__ import annotations

from sciteam.board import WorkBoard
from sciteam.depgraph import DepGraph
from sciteam.models import DepEdge, DepKind, WorkItem, WorkItemState


def test_role_pipeline_uses_waiting_deps_not_blocked():
    board = WorkBoard()
    deps = DepGraph()
    a = board.post(WorkItem(id="a", prompt="gen", role_tags=["generation"]))
    b = board.post(WorkItem(id="b", prompt="ref", role_tags=["reflection"]))
    deps.add_edge(DepEdge(pred="a", succ="b", kind=DepKind.FS))
    deps.refresh_ready(board)
    assert board.get("a").state == WorkItemState.READY
    assert board.get("b").state == WorkItemState.WAITING_DEPS
    assert "dependencies" in (board.get("b").error or "")

    board.claim("a", "generation")
    board.mark_running("a")
    board.complete("a", result="ok")
    deps.refresh_ready(board)
    assert board.get("b").state == WorkItemState.READY
    assert board.get("b").error is None


def test_hard_blocked_not_auto_cleared_by_deps():
    board = WorkBoard()
    deps = DepGraph()
    board.post(WorkItem(id="a", prompt="x"))
    board.post(WorkItem(id="b", prompt="y"))
    deps.add_edge(DepEdge(pred="a", succ="b", kind=DepKind.FS))
    board.block("b", error="merge conflict")
    board.claim("a", "w")
    board.mark_running("a")
    board.complete("a", result="ok")
    deps.refresh_ready(board)
    assert board.get("b").state == WorkItemState.BLOCKED
    assert board.get("b").error == "merge conflict"
    assert board.unblock("b") is not None
    assert board.get("b").state == WorkItemState.READY


def test_quiescent_excludes_waiting_deps():
    board = WorkBoard()
    board.post(WorkItem(id="a", prompt="x"))
    board.claim("a", "g")
    board.complete("a", result="ok")
    board.post(WorkItem(id="b", prompt="y"))
    board.wait_deps("b")
    assert board.get("b").state == WorkItemState.WAITING_DEPS
    assert board.quiescent() is False


def test_legacy_blocked_dep_message_migrates():
    board = WorkBoard()
    deps = DepGraph()
    board.post(WorkItem(id="a", prompt="gen"))
    board.post(WorkItem(id="b", prompt="ref"))
    deps.add_edge(DepEdge(pred="a", succ="b", kind=DepKind.FS))
    board.block("b", error="waiting on dependencies")
    deps.refresh_ready(board)
    assert board.get("b").state == WorkItemState.WAITING_DEPS
    board.claim("a", "generation")
    board.mark_running("a")
    board.complete("a", result="ok")
    deps.refresh_ready(board)
    assert board.get("b").state == WorkItemState.READY


def test_illegal_jumps_rejected():
    board = WorkBoard()
    board.post(WorkItem(id="a", prompt="x"))
    assert board.complete("a", result="nope") is None  # READY ↛ DONE
    assert board.get("a").state == WorkItemState.READY
    assert board.claim("a", "g") is not None
    assert board.mark_running("a") is not None
    assert board.complete("a", result="ok") is not None
    assert board.get("a").state == WorkItemState.DONE
    assert board.claim("a", "g") is None  # DONE ↛ CLAIMED
