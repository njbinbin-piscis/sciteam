"""Communication tools (30 §9). They reach the service through ctx.extras["comm"]."""

from typing import Any

from sciteam.comm.board import TRANSITIONS
from sciteam.comm.render import (
    address,
    conv_label,
    message_line,
    render_board,
    render_directory,
    render_task,
)
from sciteam.comm.service import CommClient, SubmitResult
from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.providers.llm.types import ToolSpec


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_DEPS = {"type": "array", "description": "predecessors: task ids, or {task, type} with type "
         "FS (default: start after it is done), SS (start after it started), FF (finish "
         "after it is done) or SF (finish after it started)",
         "items": {"anyOf": [{"type": "string"}, {"type": "object", "properties": {
             "task": _STR, "type": {"type": "string", "enum": ["FS", "SS", "FF", "SF"]}},
             "required": ["task"]}]}}


def _client(ctx: ToolContext) -> CommClient:
    return ctx.extras["comm"]


def _result(r: SubmitResult, ok_text: str) -> ToolResult:
    if r.ok:
        return ToolResult(content=ok_text, meta={"act_id": r.act_id, **r.data})
    if r.decision.decision == "pending":
        return ToolResult(content=f"pending ({r.act_id}): {r.decision.reason}. It takes effect "
                          f"once that is done; you will be notified of the outcome.",
                          meta={"act_id": r.act_id, "decision": "pending",
                                "waiting": r.decision.waiting})
    d = r.decision
    rule = f" ({d.rule})" if d.rule else ""
    return ToolResult(content=f"error: denied{rule}: {d.reason}", is_error=True,
                      meta={"act_id": r.act_id, "decision": d.decision, "rule": d.rule})


class SendMessageTool:
    readonly = False
    spec = ToolSpec(
        name="send_message",
        description=("Send a message. to: '@member' for a private message, '#room' for a room, "
                     "or a task id (e.g. 'T3') for that task's discussion. Mention people with "
                     "@id in the text (or list them in mentions) to notify them. reply_to: a "
                     "message id (e.g. 'm12') to answer in its thread. Keep messages short; "
                     "put long content in a workspace file and attach its path."),
        parameters=_obj({"to": _STR, "text": _STR, "reply_to": _STR, "mentions": _STRS,
                         "attachments": _STRS}, ["to", "text"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("post_message", args)
        extra = f"; notified {', '.join('@' + m for m in r.data.get('mentions', []))}" \
            if r.ok and r.data.get("mentions") else ""
        return _result(r, f"sent {r.data.get('msg_id')} to {r.data.get('conv')}{extra}")


class ReadConversationTool:
    readonly = True
    spec = ToolSpec(
        name="read_conversation",
        description=("Read messages of a conversation you belong to, newest last. conv: "
                     "'@member', '#room' or a task id. thread: a message id to read only that "
                     "thread. before: a message id to page back. limit: default 30."),
        parameters=_obj({"conv": _STR, "thread": _STR, "before": _STR,
                         "limit": {"type": "integer"}}, ["conv"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        c = _client(ctx)
        svc = c.service
        try:
            cid, conv, _ = svc.resolve_target(c.actor, args["conv"])
        except Exception as e:  # Reject carries a decision
            reason = getattr(getattr(e, "decision", None), "reason", str(e))
            return ToolResult(content=f"error: {reason}", is_error=True)
        if conv is None:
            return ToolResult(content=f"no messages yet with {args['conv']}")
        if not svc.can_read(c.actor, cid):
            return ToolResult(content=f"error: you are not a member of {cid}", is_error=True)
        msgs = c.view.messages.get(cid, [])
        if args.get("thread"):
            msgs = [m for m in msgs if m.thread == args["thread"]]
        if args.get("before"):
            anchor = c.view.msg_index.get(args["before"])
            if anchor is not None:
                msgs = [m for m in msgs if m.seq < anchor.seq]
        limit = max(1, min(int(args.get("limit") or 30), 200))
        shown = msgs[-limit:]
        head = (f"{conv_label(conv, c.actor)} — members: "
                f"{', '.join('@' + m for m in sorted(conv.members))}; showing {len(shown)} of "
                f"{len(msgs)} messages; reply with send_message(to='{address(conv, c.actor)}')")
        if len(msgs) > len(shown) and shown:
            head += f"; older: before='{shown[0].msg_id}'"
        return ToolResult(content="\n".join([head, *(message_line(m) for m in shown)]))


class ListConversationsTool:
    readonly = True
    spec = ToolSpec(name="list_conversations",
                    description="List the conversations you belong to with message counts.",
                    parameters=_obj({}, []))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        c = _client(ctx)
        convs = [cv for cv in c.view.convs.values() if c.actor in cv.members]
        convs.sort(key=lambda cv: -cv.last_seq)
        if not convs:
            return ToolResult(content="You are not in any conversation yet.")
        lines = []
        for cv in convs:
            last = c.view.messages.get(cv.id, [])[-1:] or [None]
            tail = f"; last: {message_line(last[0])[:160]}" if last[0] else ""
            lines.append(f"- {conv_label(cv, c.actor)} (to='{address(cv, c.actor)}'): "
                         f"{cv.n_messages} messages{tail}")
        return ToolResult(content="\n".join(lines))


class DirectoryTool:
    readonly = True
    spec = ToolSpec(name="directory",
                    description=("Members of the team and whom you may message, mention or "
                                 "assign work to under the current communication policy."),
                    parameters=_obj({}, []))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        c = _client(ctx)
        return ToolResult(content=render_directory(c.actor, c.view, c.service.gate))


class CreateRoomTool:
    readonly = False
    spec = ToolSpec(name="create_room",
                    description="Create a chat room with the given members (you are added).",
                    parameters=_obj({"name": _STR, "members": _STRS, "topic": _STR},
                                    ["name", "members"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("create_room", args)
        return _result(r, f"created #{str(args['name']).lstrip('#')}")


class RoomMemberTool:
    readonly = False

    def __init__(self, remove: bool):
        self.kind = "remove_member" if remove else "add_member"
        self.spec = ToolSpec(name=self.kind,
                             description=f"{'Remove a member from' if remove else 'Add a member to'}"
                                         f" a room you belong to.",
                             parameters=_obj({"room": _STR, "member": _STR}, ["room", "member"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit(self.kind, args)
        return _result(r, f"{self.kind.replace('_', ' ')}: {args['member']} in {args['room']}")


class TaskCreateTool:
    readonly = False
    spec = ToolSpec(
        name="task_create",
        description=("Create a task on the board (its discussion is the conversation with the "
                     "same id). Optional: parent (WBS), deps, assignee, reviewer (if set, submit "
                     "goes to review; otherwise submit completes it), estimate_h (used for the "
                     "critical path), labels, watchers, acceptance (a shell command that checks the "
                     "deliverable; only you, the creator, can change it later)."),
        parameters=_obj({"title": _STR, "description": _STR, "parent": _STR, "deps": _DEPS,
                         "assignee": _STR, "reviewer": _STR, "estimate_h": {"type": "number"},
                         "labels": _STRS, "watchers": _STRS, "acceptance": _STR},
                        ["title", "description"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("task_create", args)
        return _result(r, f"created {r.data.get('task')}")


class TaskTransitionTool:
    readonly = False
    spec = ToolSpec(
        name="task_transition",
        description=("Move a task through its lifecycle: assign(to) | claim | unassign | start "
                     "| block(note) | unblock | submit(artifacts, commit, note) | approve(note) | "
                     "reject(note) | reopen(note) | cancel(note). start needs FS/SS "
                     "predecessors satisfied; submit needs FF/SF predecessors satisfied. commit "
                     "is the git revision in /workspace that holds the deliverable. The note "
                     "is also posted to the task's discussion."),
        parameters=_obj({"task": _STR, "transition": {"type": "string",
                                                      "enum": sorted(TRANSITIONS)},
                         "to": _STR, "note": _STR, "artifacts": _STRS, "commit": _STR},
                        ["task", "transition"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("task_transition", args)
        return _result(r, f"{r.data.get('task')} is now {r.data.get('status')}")


class TaskUpdateTool:
    readonly = False
    spec = ToolSpec(
        name="task_update",
        description="Edit a task's title, description, deps, reviewer, watchers, estimate_h, "
                    "labels or acceptance (creator only).",
        parameters=_obj({"task": _STR, "title": _STR, "description": _STR, "deps": _DEPS,
                         "reviewer": _STR, "watchers": _STRS, "estimate_h": {"type": "number"},
                         "labels": _STRS, "acceptance": _STR}, ["task"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("task_update", args)
        return _result(r, f"updated {r.data.get('task')}")


class TaskBoardTool:
    readonly = True
    spec = ToolSpec(
        name="task_board",
        description=("The kanban board: tasks by status with assignees, dependency readiness "
                     "and, when estimates exist, the critical path. mine=true shows only tasks "
                     "you created, own or review."),
        parameters=_obj({"mine": {"type": "boolean"}, "include_closed": {"type": "boolean"}},
                        []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        c = _client(ctx)
        return ToolResult(content=render_board(c.view, include_closed=bool(
            args.get("include_closed")), mine=c.actor if args.get("mine") else None))


class TaskShowTool:
    readonly = True
    spec = ToolSpec(name="task_show",
                    description="Details, dependencies, history and recent discussion of a task.",
                    parameters=_obj({"task": _STR}, ["task"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        c = _client(ctx)
        t = c.view.resolve_task(args["task"])
        if t is None:
            return ToolResult(content=f"error: unknown task '{args['task']}'", is_error=True)
        return ToolResult(content=render_task(t, c.view))


COMM_TOOLS = {t.spec.name: t for t in (
    SendMessageTool(), ReadConversationTool(), ListConversationsTool(), DirectoryTool(),
    CreateRoomTool(), RoomMemberTool(remove=False), RoomMemberTool(remove=True),
    TaskCreateTool(), TaskTransitionTool(), TaskUpdateTool(), TaskBoardTool(), TaskShowTool(),
)}
