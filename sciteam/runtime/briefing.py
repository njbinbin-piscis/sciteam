"""Activation briefings and mid-activation deliveries (docs/31-runtime.md §3.3–3.4)."""

from collections import defaultdict

from pydantic import BaseModel

from sciteam.comm.model import Member, Message
from sciteam.comm.policy import PolicyGate
from sciteam.comm.render import address, conv_label, message_line, render_directory, task_line
from sciteam.comm.view import CommView
from sciteam.runtime.attention import AttentionItem

KIND_TEXT = {
    "task_assigned": "was assigned to you",
    "task_ready": "is now unblocked: its dependencies are satisfied",
    "task_open": "is open and ready to be claimed",
    "task_review": "was submitted and waits for your review",
    "task_returned": "was sent back to you",
    "task_changed": "changed status",
}


class MemoryEntry(BaseModel):
    ts: str
    outcome: str
    summary: str
    reason: str | None = None


def _messages_by_seq(view: CommView, conv: str, seqs: set[int]) -> list[Message]:
    return [m for m in view.messages.get(conv, []) if m.seq in seqs]


def render_activity(agent: str, items: list[AttentionItem], view: CommView,
                    per_conv: int) -> str:
    """New activity grouped by conversation / task, waking items first."""
    convs: dict[str, set[int]] = defaultdict(set)
    quiet: dict[str, set[int]] = defaultdict(set)
    task_items: list[AttentionItem] = []
    other: list[AttentionItem] = []
    for it in items:
        if it.ref in view.convs:
            (convs if it.wakes else quiet)[it.ref].add(it.seq)
        elif it.kind.startswith("task_"):
            task_items.append(it)
        else:
            other.append(it)
    out: list[str] = []
    for it in other:
        if it.kind == "timer":
            out.append(f"- Your scheduled wake-up fired ({it.ref}).")
        elif it.kind == "continue":
            out.append("- Continue the work you handed off at the end of your last activation "
                       "(see its summary under Memory).")
        elif it.kind == "retry":
            out.append("- Your last activation ended with an error (see Memory). Check the "
                       "state of your work and continue.")
        elif it.note:
            out.append(f"- {it.note}")
    seen_tasks: set[str] = set()
    for it in task_items:
        t = view.tasks.get(it.ref)
        if t is None or (t.id, it.kind) in seen_tasks:
            continue
        seen_tasks.add((t.id, it.kind))
        out.append(f"- Task {t.id} {KIND_TEXT.get(it.kind, it.kind)}: {task_line(t, view)}")
    for cid, seqs in convs.items():
        conv = view.convs[cid]
        msgs = _messages_by_seq(view, cid, seqs | quiet.pop(cid, set()))
        shown = msgs[-per_conv:]
        head = f"### {conv_label(conv, agent)} (reply: send_message(to='{address(conv, agent)}'))"
        if len(msgs) > len(shown):
            head += f" — {len(msgs) - len(shown)} earlier new messages: read_conversation"
        out += [head, *(message_line(m) for m in shown)]
    for cid, seqs in quiet.items():
        conv = view.convs[cid]
        msgs = _messages_by_seq(view, cid, seqs)
        if msgs:
            last = message_line(msgs[-1])
            out.append(f"- {conv_label(conv, agent)}: {len(msgs)} new message(s) that did not "
                       f"mention you; latest: {last[:300]}")
    return "\n".join(out) if out else "(nothing new)"


def render_briefing(*, member: Member, items: list[AttentionItem], view: CommView,
                    gate: PolicyGate, goal: str, memory: list[MemoryEntry],
                    per_conv: int, now: str,
                    sections: list[tuple[str, str]] = ()) -> str:
    my_tasks = [t for t in view.tasks.values()
                if member.id in (t.assignee, t.reviewer, t.created_by)
                and t.status not in ("done", "cancelled")]
    parts = [
        f"# Activation briefing for @{member.id} ({now})",
        "## Team goal",
        goal.strip() or "(none given)",
        "## Communication policy",
        gate.policy.description.strip(),
        render_directory(member.id, view, gate),
        "## Your open tasks",
        "\n".join("- " + task_line(t, view) for t in my_tasks) or "(none)",
        "## Memory: your recent activations",
        "\n".join(f"- {m.ts} [{m.outcome}{'/' + m.reason if m.reason else ''}] {m.summary}"
                  for m in memory) or (
            "(none: by design each of your activations starts without notes from earlier ones)"
            if member.attrs.get("memory_activations") == "0" else "(this is your first activation)"),
        *(part for title, body in sections for part in (f"## {title}", body)),
        "## New since your last activation",
        render_activity(member.id, items, view, per_conv),
        "## What to do now",
        "Handle the new activity above according to your role and the team goal, using the "
        "tools. Messages you receive while working are appended as [Incoming] notes. When you "
        "have done what you can for now, call finish: outcome=completed if your part is done "
        "for now, need_input if you are waiting for someone's answer (ask them first), "
        "blocked if you cannot proceed (tell whoever can unblock you first), handoff if you "
        "need another activation to continue your own work.",
    ]
    return "\n\n".join(parts)


def render_incoming(agent: str, items: list[AttentionItem], view: CommView,
                    per_conv: int) -> str:
    return ("[Incoming] New activity while you were working:\n"
            + render_activity(agent, items, view, per_conv)
            + "\nDeal with it when appropriate; you do not need to stop your current work.")
