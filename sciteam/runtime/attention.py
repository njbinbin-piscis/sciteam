"""Attention items derived from domain events (docs/31-runtime.md §2). Pure functions."""

from datetime import datetime

from pydantic import BaseModel

from sciteam.comm.board import dep_satisfied, successors, unmet
from sciteam.comm.model import HUMAN_ALIAS, TERMINAL
from sciteam.comm.policy import Check, CommPolicy, PolicyGate
from sciteam.comm.view import CommView
from sciteam.kernel.events import Event

# When one event gives an agent several reasons to look, the first listed kind is kept.
PRIORITY = ("mention", "dm", "task_review", "task_returned", "task_assigned", "task_ready",
            "task_open", "thread_reply", "human", "timer", "continue", "retry", "task_changed",
            "room")
ALWAYS_WAKE = ("continue", "retry")


class AttentionItem(BaseModel):
    agent: str
    kind: str
    seq: int
    ts: datetime
    wakes: bool
    ref: str = ""        # conversation id or task id
    note: str = ""       # what to tell the agent, for items raised by extensions


def agent_of_activation_stream(stream: str) -> str | None:
    # agent/<id>/act/<activation_id>
    parts = stream.split("/")
    if len(parts) == 4 and parts[0] == "agent" and parts[2] == "act":
        return parts[1]
    return None


def _claimers(view: CommView, gate: PolicyGate, task_id: str) -> list[str]:
    t = view.tasks[task_id]
    return [m.id for m in view.agents()
            if gate.can(Check(act="task_transition", transition="claim", actor=m.id, task=t),
                        view)]


def _open_and_ready(view: CommView, task_id: str) -> bool:
    t = view.tasks.get(task_id)
    return bool(t and t.status == "open" and not t.assignee
                and not unmet(t, view.tasks, "start"))


def attention_for(ev: Event, view: CommView, policy: CommPolicy, gate: PolicyGate
                  ) -> list[AttentionItem]:
    """Items caused by `ev`; `view` must already include `ev`."""
    reasons: dict[str, tuple[set[str], str]] = {}

    def add(agent: str | None, kind: str, ref: str = "") -> None:
        if not agent or agent == ev.actor or agent not in view.members:
            return
        reasons.setdefault(agent, (set(), ref))[0].add(kind)

    p = ev.payload
    if ev.type == "message_posted":
        m = p["message"]
        conv = view.convs[m["conv"]]
        author = view.members.get(m["author"])
        from_human = author is not None and author.kind == "human"
        for mid in m["mentions"]:
            if mid == HUMAN_ALIAS:
                for h in view.humans():
                    add(h.id, "mention", conv.id)
            else:
                add(mid, "mention", conv.id)
        if m.get("reply_to"):
            for poster in view.thread_posters(conv.id, m["thread"]):
                add(poster, "thread_reply", conv.id)
        for member in conv.members:
            if conv.kind == "dm":
                add(member, "dm", conv.id)
            else:
                add(member, "human" if from_human else "room", conv.id)
    elif ev.type == "task_status_changed":
        t = view.tasks[p["task_id"]]
        tr = p["transition"]
        if tr in ("assign", "claim"):
            add(t.assignee, "task_assigned", t.id)
        elif tr == "submit" and p["to"] == "in_review":
            add(t.reviewer, "task_review", t.id)
        elif tr in ("reject", "reopen"):
            add(t.assignee, "task_returned", t.id)
        if tr in ("submit", "approve", "block", "cancel", "reopen", "unassign") or \
                p["to"] == "done":
            for w in {t.created_by, *t.watchers}:
                add(w, "task_changed", t.id)
            if tr == "cancel":
                add(t.assignee, "task_changed", t.id)
        reopened = tr == "unassign" or (tr == "reopen" and p["to"] == "open")
        if reopened and _open_and_ready(view, t.id):
            for c in _claimers(view, gate, t.id):
                add(c, "task_open", t.id)
        _readiness(ev, view, gate, add)
    elif ev.type == "task_created":
        t = view.tasks[p["task"]["id"]]
        if _open_and_ready(view, t.id):
            for c in _claimers(view, gate, t.id):
                add(c, "task_open", t.id)
    elif ev.type == "timer_fired":
        reasons[p["agent"]] = ({"timer"}, p.get("timer_id", ""))
    elif ev.type == "activation_finished":
        agent = agent_of_activation_stream(ev.stream)
        if agent:
            if p["outcome"] in ("handoff", "budget_exhausted"):
                reasons[agent] = ({"continue"}, "")
            elif p["outcome"] == "error" and p.get("reason") != "cancelled":
                reasons[agent] = ({"retry"}, "")

    out = []
    for agent, (kinds, ref) in reasons.items():
        kind = next(k for k in PRIORITY if k in kinds)
        wakes = kind in ALWAYS_WAKE or any(k in policy.wake_on for k in kinds)
        if wakes:
            kind = next(k for k in PRIORITY if k in kinds and
                        (k in policy.wake_on or k in ALWAYS_WAKE))
        if kind == "task_assigned" and kinds == {"task_assigned"}:
            t = view.tasks.get(ref)
            if t is not None and unmet(t, view.tasks, "start"):
                wakes = False  # task_ready wakes the assignee once it can start
        out.append(AttentionItem(agent=agent, kind=kind, seq=ev.seq, ts=ev.ts, wakes=wakes,
                                 ref=ref))
    return out


def _readiness(ev: Event, view: CommView, gate: PolicyGate, add) -> None:
    """Successors whose dependency on the changed task just became satisfied."""
    p = ev.payload
    pred = view.tasks[p["task_id"]]
    before = pred.model_copy(update={"status": p["frm"]})
    for s in successors(view.tasks, pred.id):
        if s.status in TERMINAL:
            continue
        deps = [d for d in s.deps if d.task == pred.id]
        newly = any(dep_satisfied(d, view.tasks) and not dep_satisfied(d, {pred.id: before})
                    for d in deps)
        if not newly:
            continue
        start_ready = s.status == "assigned" and not unmet(s, view.tasks, "start")
        submit_ready = s.status in ("in_progress", "blocked") and \
            not unmet(s, view.tasks, "submit")
        if start_ready or submit_ready:
            add(s.assignee, "task_ready", s.id)
        elif _open_and_ready(view, s.id):
            for c in _claimers(view, gate, s.id):
                add(c, "task_open", s.id)
        if pred.status == "cancelled":
            add(s.assignee, "task_changed", s.id)
            add(s.created_by, "task_changed", s.id)


def still_relevant(item: AttentionItem, view: CommView) -> bool:
    """Task items can go stale before delivery (someone else claimed it, it was reassigned)."""
    t = view.tasks.get(item.ref) if item.kind.startswith("task_") else None
    if item.kind == "task_open":
        return _open_and_ready(view, item.ref)
    if item.kind in ("task_assigned", "task_ready", "task_returned"):
        return bool(t and t.assignee == item.agent and t.status not in TERMINAL)
    if item.kind == "task_review":
        return bool(t and t.reviewer == item.agent and t.status == "in_review")
    return True
