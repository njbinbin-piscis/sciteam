"""Work-item dependency graph: FS / FF / SS / SF ready-set + critical path."""

from __future__ import annotations

from collections import defaultdict, deque

from sciteam.board import WorkBoard
from sciteam.models import DepEdge, DepKind, WorkItem, WorkItemState

_STARTED = {
    WorkItemState.CLAIMED,
    WorkItemState.RUNNING,
    WorkItemState.DONE,
    WorkItemState.FAILED,
    WorkItemState.SUSPENDED,
}
_FINISHED = {WorkItemState.DONE, WorkItemState.FAILED}


def _edge_satisfied(pred: WorkItem, kind: DepKind) -> bool:
    if kind == DepKind.FS:
        return pred.state in _FINISHED
    if kind == DepKind.FF:
        return pred.state in _FINISHED
    if kind == DepKind.SS:
        return pred.state in _STARTED
    if kind == DepKind.SF:
        return pred.state in _STARTED
    return False


class DepGraph:
    def __init__(self, edges: list[DepEdge] | tuple[DepEdge, ...] | None = None) -> None:
        self._edges: list[DepEdge] = list(edges or [])

    @property
    def edges(self) -> list[DepEdge]:
        return list(self._edges)

    def add_edge(self, edge: DepEdge) -> None:
        self._edges.append(edge)

    def extend(self, edges: list[DepEdge] | tuple[DepEdge, ...]) -> None:
        self._edges.extend(edges)

    def clear(self) -> None:
        self._edges.clear()

    def predecessors(self, item_id: str) -> list[DepEdge]:
        return [e for e in self._edges if e.succ == item_id]

    def is_unblocked(self, item: WorkItem, board: WorkBoard) -> bool:
        for edge in self.predecessors(item.id):
            pred = board.get(edge.pred)
            if pred is None:
                return False
            if not _edge_satisfied(pred, edge.kind):
                return False
            # FF/SF also constrain the successor's finish; readiness to *start*
            # still requires pred condition above. FF means succ cannot finish
            # before pred; we allow start when pred finished for FF as FS-like
            # start gate for simplicity in the employment substrate.
        return True

    def refresh_ready(self, board: WorkBoard) -> list[str]:
        """WAITING_DEPS↔READY according to dep satisfaction.

        Ordinary FS waits use ``waiting_deps`` (soft). Hard ``blocked`` (merge /
        policy) is left alone so it is not auto-cleared by the dep compiler.
        Legacy boards that stored dep-waits as ``blocked`` + the dependencies
        message are migrated to ``waiting_deps``.
        """
        promoted: list[str] = []
        for item in board.all_items():
            if item.state in {
                WorkItemState.CLAIMED,
                WorkItemState.RUNNING,
                WorkItemState.DONE,
                WorkItemState.FAILED,
                WorkItemState.SUSPENDED,
            }:
                continue
            legacy_dep_block = (
                item.state == WorkItemState.BLOCKED
                and not item.metadata.get("hard_blocked")
                and (
                    "waiting on dependencies" in str(item.error or "")
                    or bool(self.predecessors(item.id))
                )
            )
            if item.state == WorkItemState.BLOCKED and not legacy_dep_block:
                continue
            ok = self.is_unblocked(item, board)
            if ok and item.state in {
                WorkItemState.WAITING_DEPS,
                WorkItemState.BLOCKED,
            }:
                board.set_ready([item.id])
                promoted.append(item.id)
            elif ok and item.state == WorkItemState.READY:
                continue
            elif not ok and item.state == WorkItemState.READY or not ok and legacy_dep_block:
                board.wait_deps(item.id, error="waiting on dependencies")
        return promoted

    def ready_set(self, board: WorkBoard) -> list[WorkItem]:
        self.refresh_ready(board)
        items = [
            i
            for i in board.all_items()
            if i.state == WorkItemState.READY and self.is_unblocked(i, board)
        ]
        items.sort(key=lambda i: (-i.priority, i.wave, i.id))
        return items

    def critical_path_length(self, board: WorkBoard) -> float:
        """Longest path weight among items (default weight 1.0)."""
        items = {i.id: i for i in board.all_items()}
        if not items:
            return 0.0
        succ: dict[str, list[str]] = defaultdict(list)
        indeg: dict[str, int] = {i: 0 for i in items}
        for edge in self._edges:
            if edge.pred in items and edge.succ in items:
                succ[edge.pred].append(edge.succ)
                indeg[edge.succ] = indeg.get(edge.succ, 0) + 1
                indeg.setdefault(edge.pred, indeg.get(edge.pred, 0))
        # Kahn longest path
        dist = {i: float(items[i].weight) for i in items}
        q = deque([i for i, d in indeg.items() if d == 0])
        seen = 0
        while q:
            u = q.popleft()
            seen += 1
            for v in succ.get(u, []):
                dist[v] = max(dist[v], dist[u] + float(items[v].weight))
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        if seen < len(items):
            # cycle — fall back to sum of weights
            return sum(float(items[i].weight) for i in items)
        return max(dist.values()) if dist else 0.0

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self._edges]

    def load_list(self, rows: list[dict]) -> None:
        self._edges = [DepEdge.from_dict(r) for r in rows]


def compile_barrier_waves(item_ids_by_wave: list[list[str]]) -> list[DepEdge]:
    """Compile barrier rounds into FS edges across consecutive waves."""
    edges: list[DepEdge] = []
    for w in range(1, len(item_ids_by_wave)):
        prev = item_ids_by_wave[w - 1]
        curr = item_ids_by_wave[w]
        for p in prev:
            for c in curr:
                edges.append(DepEdge(pred=p, succ=c, kind=DepKind.FS))
    return edges
