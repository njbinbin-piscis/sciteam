"""Task state machine, dependency semantics, cycle check and CPM (30 §5)."""

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel

from sciteam.comm.model import STARTED, TERMINAL, Dependency, Task

Gate = Literal["start", "submit"]

# transition -> (allowed source states, target state). submit/reopen targets depend on the task.
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "assign": (frozenset({"open", "assigned"}), "assigned"),
    "claim": (frozenset({"open"}), "assigned"),
    "unassign": (frozenset({"assigned"}), "open"),
    "start": (frozenset({"assigned"}), "in_progress"),
    "block": (frozenset({"in_progress"}), "blocked"),
    "unblock": (frozenset({"blocked"}), "in_progress"),
    "submit": (frozenset({"in_progress"}), "in_review"),
    "approve": (frozenset({"in_review"}), "done"),
    "reject": (frozenset({"in_review"}), "in_progress"),
    "reopen": (frozenset({"done"}), "assigned"),
    "cancel": (frozenset({"open", "assigned", "in_progress", "blocked", "in_review"}),
               "cancelled"),
}
START_GATING = ("FS", "SS")
FINISH_GATING = ("FF", "SF")


def target_state(task: Task, transition: str) -> str:
    to = TRANSITIONS[transition][1]
    if transition == "submit" and not task.reviewer:
        return "done"
    if transition == "reopen" and not task.assignee:
        return "open"
    return to


def dep_satisfied(dep: Dependency, tasks: Mapping[str, Task]) -> bool:
    pred = tasks.get(dep.task)
    if pred is None or pred.status == "cancelled":
        return True  # released; the parties are notified and decide (30 §5.3)
    if dep.type in ("FS", "FF"):
        return pred.status == "done"
    return pred.status in STARTED


def unmet(task: Task, tasks: Mapping[str, Task], gate: Gate) -> list[Dependency]:
    kinds = START_GATING if gate == "start" else FINISH_GATING
    return [d for d in task.deps if d.type in kinds and not dep_satisfied(d, tasks)]


def check_transition(task: Task, transition: str, tasks: Mapping[str, Task], *,
                     to: str | None = None, members: Iterable[str] = ()) -> str | None:
    """Structural legality only (who may act is the policy's job). None = legal."""
    if transition not in TRANSITIONS:
        return f"unknown transition '{transition}'; one of {sorted(TRANSITIONS)}"
    sources = TRANSITIONS[transition][0]
    if task.status not in sources:
        return (f"cannot {transition} {task.short} in status '{task.status}' "
                f"(allowed from: {', '.join(sorted(sources))})")
    if transition == "assign":
        if not to:
            return "assign needs 'to'"
        if to not in set(members):
            return f"unknown member '{to}'"
    if transition == "claim" and task.assignee:
        return f"{task.short} is already assigned to {task.assignee}"
    if transition in ("approve", "reject") and not task.reviewer:
        return f"{task.short} has no reviewer"
    gate: Gate | None = "start" if transition == "start" else \
        "submit" if transition == "submit" else None
    if gate:
        missing = unmet(task, tasks, gate)
        if missing:
            names = ", ".join(f"{d.type} {tasks[d.task].short if d.task in tasks else d.task}"
                              for d in missing)
            return f"cannot {transition} {task.short}: unmet dependencies ({names})"
    return None


def would_cycle(tasks: Mapping[str, Task], task_id: str, deps: Iterable[Dependency]) -> bool:
    """True if giving task_id these predecessors creates a cycle."""
    preds = {tid: [d.task for d in t.deps] for tid, t in tasks.items()}
    preds[task_id] = [d.task for d in deps]
    seen: set[str] = set()
    stack = list(preds[task_id])
    while stack:
        cur = stack.pop()
        if cur == task_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(preds.get(cur, []))
    return False


def successors(tasks: Mapping[str, Task], task_id: str) -> list[Task]:
    return [t for t in tasks.values() if any(d.task == task_id for d in t.deps)]


def descendants(tasks: Mapping[str, Task], task_id: str) -> list[Task]:
    out, frontier = [], [task_id]
    while frontier:
        cur = frontier.pop()
        kids = [t for t in tasks.values() if t.parent == cur]
        out += kids
        frontier += [k.id for k in kids]
    return out


class CpmRow(BaseModel):
    task: str
    es: float
    ef: float
    ls: float
    lf: float
    float_h: float
    critical: bool
    estimated: bool


def cpm(tasks: Mapping[str, Task]) -> tuple[list[CpmRow], float]:
    """Critical path method with FS/SS/FF/SF precedence (zero lag). Missing estimates count 0.
    WBS summary tasks (those with subtasks) are not scheduled; their children are."""
    parents = {t.parent for t in tasks.values() if t.parent and t.status != "cancelled"}
    live = {tid: t for tid, t in tasks.items() if t.status != "cancelled" and tid not in parents}
    dur = {tid: float(t.estimate_h or 0.0) for tid, t in live.items()}
    order = _topo(live)
    es: dict[str, float] = {}
    for tid in order:
        d = dur[tid]
        start = 0.0
        for dep in live[tid].deps:
            a = dep.task
            if a not in live:
                continue
            start = max(start, {"FS": es[a] + dur[a], "SS": es[a],
                                "FF": es[a] + dur[a] - d, "SF": es[a] - d}[dep.type])
        es[tid] = max(start, 0.0)
    end = max((es[t] + dur[t] for t in order), default=0.0)
    lf = {tid: end for tid in order}
    for tid in reversed(order):
        for dep in live[tid].deps:
            a = dep.task
            if a not in live:
                continue
            ls_b, lf_b = lf[tid] - dur[tid], lf[tid]
            bound = {"FS": ls_b, "SS": ls_b + dur[a], "FF": lf_b, "SF": lf_b + dur[a]}[dep.type]
            lf[a] = min(lf[a], bound)
    rows = []
    for tid in order:
        fl = round(lf[tid] - dur[tid] - es[tid], 6)
        rows.append(CpmRow(task=tid, es=es[tid], ef=es[tid] + dur[tid], ls=lf[tid] - dur[tid],
                           lf=lf[tid], float_h=fl, critical=abs(fl) < 1e-9,
                           estimated=live[tid].estimate_h is not None))
    return rows, end


def _topo(tasks: Mapping[str, Task]) -> list[str]:
    indeg = {tid: 0 for tid in tasks}
    for tid, t in tasks.items():
        indeg[tid] = len({d.task for d in t.deps if d.task in tasks})
    ready = sorted(tid for tid, n in indeg.items() if n == 0)
    out = []
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        for t in successors(tasks, cur):
            if t.id in indeg:
                indeg[t.id] -= 1
                if indeg[t.id] == 0:
                    ready.append(t.id)
    return out


def is_open(task: Task) -> bool:
    return task.status not in TERMINAL
