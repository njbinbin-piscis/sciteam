"""Shared work board — atomic claim surface for employment loop."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from sciteam.models import WorkItem, WorkItemState

# Legal transitions (to → allowed from). Terminal states have no outbound edges
# except via a new work item. Soft wait is distinct from hard block.
_TRANSITIONS: dict[WorkItemState, frozenset[WorkItemState]] = {
    WorkItemState.READY: frozenset(
        {
            WorkItemState.WAITING_DEPS,
            WorkItemState.BLOCKED,
            WorkItemState.SUSPENDED,
        }
    ),
    WorkItemState.WAITING_DEPS: frozenset(
        {WorkItemState.READY, WorkItemState.BLOCKED}  # BLOCKED: legacy dep-wait migrate
    ),
    WorkItemState.CLAIMED: frozenset({WorkItemState.READY}),
    WorkItemState.RUNNING: frozenset({WorkItemState.CLAIMED}),
    WorkItemState.SUSPENDED: frozenset({WorkItemState.CLAIMED, WorkItemState.RUNNING}),
    WorkItemState.BLOCKED: frozenset(
        {
            WorkItemState.READY,
            WorkItemState.WAITING_DEPS,
            WorkItemState.CLAIMED,
            WorkItemState.RUNNING,
            WorkItemState.SUSPENDED,
        }
    ),
    WorkItemState.DONE: frozenset({WorkItemState.CLAIMED, WorkItemState.RUNNING}),
    WorkItemState.FAILED: frozenset({WorkItemState.CLAIMED, WorkItemState.RUNNING}),
}


class WorkBoard:
    def __init__(self) -> None:
        self._items: dict[str, WorkItem] = {}
        self._lock = threading.Lock()

    def _transit(self, item: WorkItem, new_state: WorkItemState, *, force: bool = False) -> bool:
        if item.state == new_state:
            return True
        if force:
            item.state = new_state
            return True
        allowed = _TRANSITIONS.get(new_state)
        if allowed is None or item.state not in allowed:
            return False
        item.state = new_state
        return True

    def post(self, item: WorkItem) -> WorkItem:
        with self._lock:
            if item.id in self._items:
                raise ValueError(f"work item already posted: {item.id}")
            if item.state not in {
                WorkItemState.BLOCKED,
                WorkItemState.WAITING_DEPS,
                WorkItemState.SUSPENDED,
            }:
                item.state = WorkItemState.READY
            self._items[item.id] = item
            return item

    def get(self, item_id: str) -> WorkItem | None:
        with self._lock:
            return self._items.get(item_id)

    def all_items(self) -> list[WorkItem]:
        with self._lock:
            return list(self._items.values())

    def by_state(self, *states: WorkItemState) -> list[WorkItem]:
        wanted = set(states)
        with self._lock:
            return [i for i in self._items.values() if i.state in wanted]

    def claim(self, item_id: str, agent_key: str) -> WorkItem | None:
        """Atomically claim a READY item. Returns None if lost the race."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if item.state != WorkItemState.READY:
                return None
            if not self._transit(item, WorkItemState.CLAIMED):
                return None
            item.assignee = agent_key
            return item

    def mark_running(self, item_id: str) -> WorkItem | None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if not self._transit(item, WorkItemState.RUNNING):
                return None
            return item

    def complete(self, item_id: str, *, result: str = "") -> WorkItem | None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if not self._transit(item, WorkItemState.DONE):
                return None
            item.result = result
            item.error = None
            return item

    def fail(self, item_id: str, *, error: str) -> WorkItem | None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if not self._transit(item, WorkItemState.FAILED):
                return None
            item.error = error
            return item

    def requeue(
        self,
        item_id: str,
        *,
        reason_code: str,
        diagnosis: str,
        retry_hint: str = "",
    ) -> WorkItem | None:
        """Return an in-flight item to READY with an auditable retry record."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.state not in {
                WorkItemState.CLAIMED,
                WorkItemState.RUNNING,
            }:
                return None
            attempts = list(item.metadata.get("attempts") or [])
            retry_count = int(item.metadata.get("retry_count") or 0) + 1
            attempts.append(
                {
                    "attempt": retry_count,
                    "reason_code": str(reason_code or "recoverable_error"),
                    "diagnosis": str(diagnosis or "")[:4000],
                    "retry_hint": str(retry_hint or "")[:2000],
                }
            )
            item.metadata["retry_count"] = retry_count
            item.metadata["last_reason_code"] = str(reason_code or "recoverable_error")
            item.metadata["diagnosis"] = str(diagnosis or "")[:4000]
            item.metadata["retry_hint"] = str(retry_hint or "")[:2000]
            item.metadata["attempts"] = attempts[-10:]
            item.state = WorkItemState.READY
            item.error = str(diagnosis or reason_code or "recoverable error")
            item.assignee = None
            return item

    def suspend(self, item_id: str, *, checkpoint_ref: str | None = None) -> WorkItem | None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if not self._transit(item, WorkItemState.SUSPENDED):
                return None
            if checkpoint_ref:
                item.checkpoint_ref = checkpoint_ref
            return item

    def resume(self, item_id: str) -> WorkItem | None:
        """Return a suspended item to READY for re-claim."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if not self._transit(item, WorkItemState.READY):
                return None
            return item

    def block(self, item_id: str, *, error: str | None = None) -> WorkItem | None:
        """Hard block (merge conflict / policy). Not ordinary dependency wait."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if not self._transit(item, WorkItemState.BLOCKED):
                return None
            if "waiting on dependencies" not in str(error or ""):
                item.metadata["hard_blocked"] = True
            if error:
                item.error = error
            return item

    def wait_deps(self, item_id: str, *, error: str | None = None) -> WorkItem | None:
        """Soft wait until predecessors finish (role_pipeline / work_graph)."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if item.state == WorkItemState.WAITING_DEPS:
                item.error = error or item.error or "waiting on dependencies"
                return item
            if not self._transit(item, WorkItemState.WAITING_DEPS):
                return item
            item.error = error or "waiting on dependencies"
            return item

    def set_ready(self, item_ids: Iterable[str]) -> None:
        """Promote soft waits (and legacy dep-blocks) to READY."""
        with self._lock:
            for item_id in item_ids:
                item = self._items.get(item_id)
                if item is None:
                    continue
                if item.state == WorkItemState.WAITING_DEPS:
                    self._transit(item, WorkItemState.READY)
                    item.error = None
                elif item.state == WorkItemState.BLOCKED and not item.metadata.get("hard_blocked"):
                    # Legacy migration path
                    item.state = WorkItemState.READY
                    item.error = None

    def unblock(self, item_id: str) -> WorkItem | None:
        """Clear a hard BLOCKED item back to READY (explicit policy action)."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.state != WorkItemState.BLOCKED:
                return None
            item.state = WorkItemState.READY
            item.error = None
            item.metadata.pop("hard_blocked", None)
            return item

    def to_list(self) -> list[dict]:
        with self._lock:
            return [i.to_dict() for i in self._items.values()]

    def load_list(self, rows: list[dict]) -> None:
        with self._lock:
            self._items = {str(r["id"]): WorkItem.from_dict(r) for r in rows if r.get("id")}

    def quiescent(self) -> bool:
        """True when every item is terminal. Dependency waits are NOT quiescent."""
        with self._lock:
            terminal = {WorkItemState.DONE, WorkItemState.FAILED}
            return bool(self._items) and all(i.state in terminal for i in self._items.values())

    def has_inflight(self) -> bool:
        with self._lock:
            return any(
                i.state
                in {
                    WorkItemState.READY,
                    WorkItemState.CLAIMED,
                    WorkItemState.RUNNING,
                    WorkItemState.SUSPENDED,
                }
                for i in self._items.values()
            )
