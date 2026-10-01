"""Plain-text renderings of messages, conversations, tasks and the board for the model."""

from sciteam.comm.board import cpm, dep_satisfied, is_open, unmet
from sciteam.comm.model import Conversation, Message, Task
from sciteam.comm.policy import Check, PolicyGate
from sciteam.comm.view import CommView

COLUMNS = ("open", "assigned", "in_progress", "blocked", "in_review", "done", "cancelled")


def conv_label(conv: Conversation, viewer: str) -> str:
    if conv.kind == "dm":
        others = sorted(conv.members - {viewer})
        return "private chat with " + ", ".join("@" + o for o in others)
    if conv.kind == "room":
        return "#" + conv.id[5:]
    return conv.id


def address(conv: Conversation, viewer: str) -> str:
    """What to pass as `to` to reply into this conversation."""
    if conv.kind == "dm":
        others = sorted(conv.members - {viewer})
        return "@" + others[0] if others else conv.id
    if conv.kind == "room":
        return "#" + conv.id[5:]
    return conv.id[5:]


def message_line(m: Message) -> str:
    ts = m.ts.strftime("%m-%d %H:%M:%S") if m.ts else ""
    head = f"[{m.msg_id} {ts}] @{m.author}"
    if m.reply_to:
        head += f" (reply to {m.reply_to}, thread {m.thread})"
    line = f"{head}: {m.text}"
    if m.attachments:
        line += "\n  attachments: " + ", ".join(a.path or f"blob:{(a.blob or '')[:16]}"
                                              for a in m.attachments)
    return line


def dep_line(t: Task, view: CommView) -> str:
    if not t.deps:
        return ""
    parts = []
    for d in t.deps:
        ok = dep_satisfied(d, view.tasks)
        parts.append(f"{d.type} {d.task}{' ok' if ok else ' waiting'}")
    return "deps: " + ", ".join(parts)


def task_line(t: Task, view: CommView) -> str:
    bits = [f"{t.id} [{t.status}] {t.title}"]
    who = []
    if t.assignee:
        who.append(f"assignee @{t.assignee}")
    if t.reviewer:
        who.append(f"reviewer @{t.reviewer}")
    who.append(f"by @{t.created_by}")
    bits.append("(" + ", ".join(who) + ")")
    if t.parent:
        bits.append(f"parent {t.parent}")
    d = dep_line(t, view)
    if d:
        bits.append(d)
    if t.status == "assigned" and unmet(t, view.tasks, "start"):
        bits.append("NOT READY to start")
    if t.status == "blocked":
        bits.append(f"blocked: {t.block_reason}")
    if t.estimate_h is not None:
        bits.append(f"est {t.estimate_h:g}h")
    return " ".join(bits)


def render_board(view: CommView, *, include_closed: bool = False, mine: str | None = None) -> str:
    tasks = list(view.tasks.values())
    if mine:
        tasks = [t for t in tasks if mine in (t.assignee, t.reviewer, t.created_by)]
    if not tasks:
        return "The board is empty."
    lines = []
    for col in COLUMNS:
        if col in ("done", "cancelled") and not include_closed:
            n = sum(1 for t in tasks if t.status == col)
            if n:
                lines.append(f"## {col}: {n} task(s) (use include_closed=true to list)")
            continue
        items = [t for t in tasks if t.status == col]
        if items:
            lines.append(f"## {col}")
            lines += ["- " + task_line(t, view) for t in items]
    if any(t.estimate_h is not None for t in view.tasks.values()):
        rows, end = cpm(view.tasks)
        crit = [r.task for r in rows if r.critical and is_open(view.tasks[r.task])]
        lines.append(f"## schedule (CPM on estimate_h): project length {end:g}h; "
                     f"open critical-path tasks: {', '.join(crit) or 'none'}")
        missing = [t.id for t in view.tasks.values() if t.estimate_h is None and is_open(t)]
        if missing:
            lines.append(f"(no estimate, counted as 0h: {', '.join(missing)})")
    return "\n".join(lines)


def render_task(t: Task, view: CommView, *, messages: int = 10) -> str:
    lines = [task_line(t, view)]
    if t.description:
        lines += ["", t.description]
    if t.acceptance:
        lines.append(f"acceptance: `{t.acceptance}`")
    if t.artifacts:
        lines.append("artifacts: " + ", ".join(a.path or f"blob:{a.blob}" for a in t.artifacts))
    kids = [k for k in view.tasks.values() if k.parent == t.id]
    if kids:
        lines.append("subtasks:")
        lines += ["- " + task_line(k, view) for k in kids]
    if t.history:
        lines.append("history:")
        lines += [f"- {h.ts:%m-%d %H:%M} @{h.actor} {h.transition}: {h.frm} -> {h.to}"
                  + (f" ({h.note[:200]})" if h.note else "") for h in t.history]
    msgs = view.messages.get(t.conv, [])
    if msgs:
        lines.append(f"discussion (last {min(messages, len(msgs))} of {len(msgs)}; "
                     f"read_conversation(conv='{t.id}') for more):")
        lines += [message_line(m) for m in msgs[-messages:]]
    return "\n".join(lines)


def reach(actor: str, view: CommView, gate: PolicyGate) -> dict[str, list[str]]:
    others = [m for m in view.members if m != actor]
    dm = [o for o in others if gate.can(Check(act="post_message", actor=actor, target=o,
                                              via="dm"), view)]
    mention = [o for o in others if gate.can(Check(act="mention", actor=actor, target=o), view)]
    assign = [o for o in view.members if gate.can(
        Check(act="task_transition", transition="assign", actor=actor, target=o), view)]
    return {"dm": dm, "mention": mention, "assign": assign}


def render_directory(actor: str, view: CommView, gate: PolicyGate) -> str:
    lines = ["## members"]
    for m in view.members.values():
        bits = [f"@{m.id}", m.kind]
        if m.role:
            bits.append(f"role={m.role}")
        if m.team:
            bits.append(f"team={m.team}")
        if m.manager:
            bits.append(f"manager=@{m.manager}")
        lines.append("- " + " ".join(bits) + (" (you)" if m.id == actor else ""))
    r = reach(actor, view, gate)

    def fmt(ids: list[str]) -> str:
        return ", ".join("@" + i for i in ids) or "nobody"

    can = {name: gate.can(Check(act=name, actor=actor), view)
           for name in ("task_create", "create_room")}
    claim = gate.can(Check(act="task_transition", transition="claim", actor=actor), view)
    rooms = [conv_label(c, actor) for c in view.convs.values()
             if c.kind == "room" and actor in c.members]
    lines += [
        "## what you may do under the current communication policy",
        f"- private messages to: {fmt(r['dm'])}",
        f"- @mention: {fmt(r['mention'])}",
        f"- assign tasks to: {fmt(r['assign'])}",
        f"- create tasks: {'yes' if can['task_create'] else 'no'}; claim open tasks: "
        f"{'yes' if claim else 'no'}; create rooms: {'yes' if can['create_room'] else 'no'}",
        f"- your rooms: {', '.join(rooms) or 'none'}",
    ]
    return "\n".join(lines)
