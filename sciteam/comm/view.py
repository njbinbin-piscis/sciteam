"""CommView: roster, conversations, messages and task board folded from events (30 §10)."""

from collections import defaultdict
from datetime import datetime

from sciteam.comm.model import Attachment, Conversation, Member, Message, Task, TaskEvent
from sciteam.kernel.events import Event

COMM_EVENT_TYPES = {
    "member_registered", "member_updated", "member_removed",
    "conversation_created", "conv_member_added", "conv_member_removed", "message_posted",
    "task_created", "task_updated", "task_status_changed",
}


class CommView:
    """Incrementally applied fold. apply() must be fed every event in global seq order;
    events of other types are ignored, so feeding the whole store is always correct."""

    def __init__(self) -> None:
        self.members: dict[str, Member] = {}
        self.convs: dict[str, Conversation] = {}
        self.messages: dict[str, list[Message]] = defaultdict(list)
        self.msg_index: dict[str, Message] = {}
        self.tasks: dict[str, Task] = {}
        self.short_index: dict[str, str] = {}
        self.post_times: list[tuple[datetime, str, str]] = []  # (ts, author, conv)
        self.last_seq = 0

    # ---- queries ------------------------------------------------------------------------
    def agents(self) -> list[Member]:
        return [m for m in self.members.values() if m.kind == "agent"]

    def humans(self) -> list[Member]:
        return [m for m in self.members.values() if m.kind == "human"]

    def reports(self, mid: str) -> set[str]:
        return {m.id for m in self.members.values() if m.manager == mid}

    def resolve_task(self, ref: str) -> Task | None:
        ref = ref.strip()
        if ref.startswith("task:"):
            ref = ref[5:]
        tid = self.short_index.get(ref.upper(), ref)
        return self.tasks.get(tid)

    def thread_posters(self, conv: str, thread: str) -> set[str]:
        return {m.author for m in self.messages.get(conv, []) if m.thread == thread}

    # ---- fold ---------------------------------------------------------------------------
    def apply(self, ev: Event) -> None:
        self.last_seq = max(self.last_seq, ev.seq)
        handler = getattr(self, f"_on_{ev.type}", None)
        if handler is not None:
            handler(ev, ev.payload)

    def _on_member_registered(self, ev: Event, p: dict) -> None:
        m = Member.model_validate(p["member"])
        self.members[m.id] = m

    def _on_member_updated(self, ev: Event, p: dict) -> None:
        m = self.members[p["id"]]
        self.members[m.id] = m.model_copy(update=p["fields"])

    def _on_member_removed(self, ev: Event, p: dict) -> None:
        self.members.pop(p["id"], None)

    def _on_conversation_created(self, ev: Event, p: dict) -> None:
        c = Conversation.model_validate(p["conv"])
        self.convs[c.id] = c

    def _on_conv_member_added(self, ev: Event, p: dict) -> None:
        self.convs[p["conv"]].members.add(p["member"])

    def _on_conv_member_removed(self, ev: Event, p: dict) -> None:
        self.convs[p["conv"]].members.discard(p["member"])

    def _on_message_posted(self, ev: Event, p: dict) -> None:
        m = Message.model_validate(p["message"])
        m.seq, m.ts = ev.seq, ev.ts
        self.messages[m.conv].append(m)
        self.msg_index[m.msg_id] = m
        c = self.convs[m.conv]
        c.n_messages += 1
        c.last_seq = ev.seq
        self.post_times.append((ev.ts, m.author, m.conv))

    def _on_task_created(self, ev: Event, p: dict) -> None:
        t = Task.model_validate(p["task"])
        self.tasks[t.id] = t
        self.short_index[t.short.upper()] = t.id
        self._sync_task_conv(t)

    def _on_task_updated(self, ev: Event, p: dict) -> None:
        t = self.tasks[p["task_id"]]
        self.tasks[t.id] = t = Task.model_validate({**t.model_dump(), **p["fields"]})
        self._sync_task_conv(t)

    def _on_task_status_changed(self, ev: Event, p: dict) -> None:
        t = self.tasks[p["task_id"]]
        t.status = p["to"]
        if "assignee" in p:
            t.assignee = p["assignee"]
        if p.get("artifacts"):
            t.artifacts = [*t.artifacts, *(Attachment.model_validate(a) for a in p["artifacts"])]
        if p["transition"] == "block":
            t.block_reason = p.get("note", "")
        elif p["transition"] == "unblock":
            t.block_reason = ""
        t.history.append(TaskEvent(seq=ev.seq, ts=ev.ts, actor=ev.actor or "", frm=p["frm"],
                                   to=p["to"], transition=p["transition"],
                                   note=p.get("note", "")))
        self._sync_task_conv(t)

    def _sync_task_conv(self, t: Task) -> None:
        c = self.convs.get(t.conv)
        if c is not None:
            c.members |= t.parties()
