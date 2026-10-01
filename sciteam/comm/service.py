"""CommService: every communication Act passes one path — record, check, decide, apply (30 §6)."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from sciteam.comm.board import check_transition, descendants, target_state, would_cycle
from sciteam.comm.model import (
    HUMAN_ALIAS,
    ID_RE,
    TERMINAL,
    Act,
    Attachment,
    Conversation,
    Decision,
    Dependency,
    Member,
    Message,
    Task,
    deny,
    dm_id,
    extract_mentions,
)
from sciteam.comm.policy import Check, CommPolicy, PolicyGate
from sciteam.comm.view import COMM_EVENT_TYPES, CommView
from sciteam.kernel.clock import Clock
from sciteam.kernel.events import Event, EventStore
from sciteam.kernel.ids import new_id

SYSTEM = "system"


class Gate(Protocol):
    """Extra enforcement point consulted after the communication checks allow an Act
    (layer 3 plugs the institution here). It must be a pure function of the Act and the
    folded state, so that resolving a pending Act is just deciding it again."""

    def decide(self, act: Act, view: CommView) -> Decision: ...

    def on_pending(self, act: Act, decision: Decision,
                   view: CommView) -> list[tuple[str, str, dict]]:
        """Events to write right after a pending decision (e.g. validation requests)."""
        ...

    def rule_version(self) -> str | None: ...


class SubmitResult(BaseModel):
    act_id: str
    decision: Decision
    data: dict[str, Any] = {}

    @property
    def ok(self) -> bool:
        return self.decision.decision == "allow"


@dataclass
class Plan:
    """Domain events an allowed Act will write, in order."""
    events: list[tuple[str, str, dict]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def add(self, stream: str, type: str, payload: dict) -> None:
        self.events.append((stream, type, payload))


class Reject(Exception):
    def __init__(self, decision: Decision):
        self.decision = decision


def need(cond: bool, reason: str) -> None:
    if not cond:
        raise Reject(deny(reason, "structure"))


def _attachments(raw: Any) -> list[dict]:
    out = []
    for a in raw or []:
        att = Attachment(path=a) if isinstance(a, str) else Attachment.model_validate(a)
        out.append(att.model_dump())
    return out


class CommService:
    def __init__(self, store: EventStore, policy: CommPolicy, clock: Clock,
                 extra_gate: Gate | None = None,
                 extra_kinds: dict[str, Callable[[Act], Plan]] | None = None):
        self.store = store
        self.clock = clock
        self.set_policy(policy)
        self.extra_gate = extra_gate
        self.extra_kinds = extra_kinds or {}
        # extra kinds that still go through extra_gate (their rules may require validation)
        self.gated_kinds: set[str] = set()
        self.view = CommView()
        self.followers: list[Callable[[], None]] = []   # folds of higher layers
        self._lock = threading.RLock()
        self._cursor = 0
        self.catch_up()

    def set_policy(self, policy: CommPolicy) -> None:
        self.policy = policy
        self.gate = PolicyGate(policy, self.clock)

    def catch_up(self) -> None:
        """Fold comm events written since the last call, by any writer (other processes
        included), in global order; then let higher layers fold theirs."""
        with self._lock:
            for ev in self.store.read_global(after_seq=self._cursor, types=COMM_EVENT_TYPES):
                self.view.apply(ev)
                self._cursor = ev.seq
            for follow in self.followers:
                follow()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- writing ------------------------------------------------------------------------
    def _append(self, stream: str, type: str, payload: dict, actor: str,
                causation: int | None = None, rule_version: str | None = None) -> Event:
        ev = self.store.append(stream, type, payload, actor=actor, causation_id=causation,
                               rule_version=rule_version)
        self.catch_up()
        return ev

    def record(self, stream: str, type: str, payload: dict, actor: str = SYSTEM,
               causation: int | None = None) -> Event:
        """System-written consequences of recorded events (higher layers: validation results,
        derived facts); stamped with the rule version in force."""
        with self._lock:
            self.catch_up()
            return self._append(stream, type, payload, actor, causation, self._version())

    def _version(self) -> str | None:
        return self.extra_gate.rule_version() if self.extra_gate is not None else None

    def _decide(self, act: Act) -> tuple[Plan, Decision]:
        try:
            plan = self._plan(act)
            decision = Decision(decision="allow", rule=plan.data.pop("_rule", None))
            if self.extra_gate is not None and (act.kind not in self.extra_kinds
                                                or act.kind in self.gated_kinds):
                extra = self.extra_gate.decide(act, self.view)
                if extra.decision != "allow":
                    decision = extra
        except Reject as r:
            plan, decision = Plan(), r.decision
        return plan, decision

    def _apply(self, act: Act, plan: Plan, decision: Decision, cause: int) -> None:
        if decision.decision == "allow":
            for stream, type, payload in plan.events:
                self._append(stream, type, payload, act.actor, cause)
        elif decision.decision == "pending" and self.extra_gate is not None:
            for stream, type, payload in self.extra_gate.on_pending(act, decision, self.view):
                self._append(stream, type, payload, "system", cause)

    def submit(self, act: Act) -> SubmitResult:
        with self._lock:
            self.catch_up()
            version = self._version()
            act = act.model_copy(update={"rule_version": version})
            self._append("gate", "act_submitted", {"act": act.model_dump(mode="json")},
                         act.actor, rule_version=version)
            plan, decision = self._decide(act)
            decided = self._append("gate", "act_decided", {
                "act_id": act.act_id, "kind": act.kind, "decision": decision.decision,
                "reason": decision.reason, "rule": decision.rule,
                "waiting": decision.waiting}, act.actor, rule_version=version)
            self._apply(act, plan, decision, decided.seq)
            return SubmitResult(act_id=act.act_id, decision=decision,
                                data=plan.data if decision.decision != "deny" else {})

    def resolve(self, act: Act, cause: int) -> Decision:
        """Decide a pending Act again against the current state (40 §6.1). Still pending:
        write any new requests it now needs; otherwise record the outcome and apply it. `cause` is
        the seq of the event that made it resolvable (a validation result, an approval)."""
        with self._lock:
            self.catch_up()
            version = self._version()
            plan, decision = self._decide(act)
            if decision.decision == "pending":
                self._apply(act, plan, decision, cause)
                return decision
            resolved = self._append("gate", "act_resolved", {
                "act_id": act.act_id, "kind": act.kind, "actor": act.actor,
                "decision": decision.decision, "reason": decision.reason,
                "rule": decision.rule}, "system", cause, rule_version=version)
            self._apply(act, plan, decision, resolved.seq)
            return decision

    def _policy(self, chk: Check) -> str | None:
        d = self.gate.ask(chk, self.view)
        if d.decision != "allow":
            raise Reject(d)
        return d.rule

    policy_check = _policy   # for planners of higher layers

    def _plan(self, act: Act) -> Plan:
        need(act.actor in self.view.members, f"unknown actor '{act.actor}'")
        if act.kind in self.extra_kinds:
            return self.extra_kinds[act.kind](act)
        handler = getattr(self, f"_plan_{act.kind}", None)
        need(handler is not None, f"unknown act kind '{act.kind}'")
        return handler(act, act.params)

    # ---- messages -----------------------------------------------------------------------
    def resolve_target(self, actor: str, to: str) -> tuple[str, Conversation | None, str]:
        """Returns (conv_id, existing conv or None, peer id for dm or '')."""
        v = self.view
        to = to.strip()
        if to.startswith("@"):
            peer = to[1:]
            if peer == HUMAN_ALIAS:
                humans = [m.id for m in v.humans()]
                need(len(humans) == 1, f"@human is ambiguous; use one of {humans}")
                peer = humans[0]
            need(peer in v.members, f"unknown member '{peer}'")
            need(peer != actor, "cannot send a private message to yourself")
            cid = dm_id(actor, peer)
            return cid, v.convs.get(cid), peer
        if to.startswith("#"):
            cid = f"room:{to[1:]}"
            need(cid in v.convs, f"no room '{to}'")
            return cid, v.convs[cid], ""
        task = v.resolve_task(to)
        need(task is not None, f"unknown target '{to}'; use @member, #room or a task id")
        return task.conv, v.convs[task.conv], ""

    def _plan_post_message(self, act: Act, p: dict) -> Plan:
        v, actor, plan = self.view, act.actor, Plan()
        text = str(p.get("text", "")).strip()
        need(bool(text), "empty message")
        lim = self.policy.limits.message_chars
        need(len(text) <= lim, f"message is {len(text)} chars; the limit is {lim}. Put long "
                                f"content in a workspace file and attach its path")
        cid, conv, peer = self.resolve_target(actor, str(p.get("to", "")))
        me = v.members[actor]
        task = v.resolve_task(cid) if cid.startswith("task:") else None
        if conv is None:
            conv = Conversation(id=cid, kind="dm", members={actor, peer}, created_by=actor)
            plan.add(f"conv/{cid}", "conversation_created", {"conv": conv.model_dump(mode="json")})
        elif actor not in conv.members:
            need(conv.kind == "task" or (me.kind == "human" and self.policy.humans_see_all),
                  f"you are not a member of {cid}")
            plan.add(f"conv/{cid}", "conv_member_added", {"conv": cid, "member": actor})
        rule = self._policy(Check(act="post_message", actor=actor, target=peer or None,
                                  via=conv.kind, conv=conv, task=task))
        reply_to = p.get("reply_to")
        thread = None
        if reply_to:
            parent = v.msg_index.get(str(reply_to))
            need(parent is not None and parent.conv == cid,
                  f"reply_to '{reply_to}' is not a message in {cid}")
            thread = parent.thread
        known = set(v.members)
        explicit = [str(m).lstrip("@") for m in p.get("mentions") or []]
        for m in explicit:
            need(m in known or m == HUMAN_ALIAS, f"unknown member '@{m}' in mentions")
        mentions = [m for m in dict.fromkeys(extract_mentions(text, known) + explicit)
                    if m != actor]
        refused = [m for m in mentions if not self.gate.can(
            Check(act="mention", actor=actor, target=m, conv=conv, task=task), v)]
        if refused:
            raise Reject(deny(f"{actor} may not mention {', '.join('@' + m for m in refused)} "
                               f"under the current policy", "mention"))
        for m in mentions:
            if m == HUMAN_ALIAS or m in conv.members:
                continue
            need(conv.kind != "dm", f"@{m} cannot read this private conversation; message "
                                     f"them directly instead")
            plan.add(f"conv/{cid}", "conv_member_added", {"conv": cid, "member": m})
        limited = self.gate.rate(actor, conv, v)
        if limited is not None:
            raise Reject(limited)
        mid = f"m{len(v.msg_index) + 1}"
        msg = Message(msg_id=mid, conv=cid, author=actor, text=text, mentions=mentions,
                      reply_to=reply_to, thread=thread or mid,
                      attachments=_attachments(p.get("attachments")))
        plan.add(f"conv/{cid}", "message_posted",
                 {"message": msg.model_dump(mode="json", exclude={"seq", "ts"})})
        plan.data = {"_rule": rule, "msg_id": mid, "conv": cid, "mentions": mentions}
        return plan

    # ---- rooms --------------------------------------------------------------------------
    def _plan_create_room(self, act: Act, p: dict) -> Plan:
        v, plan = self.view, Plan()
        name = str(p.get("name", "")).lstrip("#")
        need(bool(ID_RE.match(name)), f"invalid room name '{name}'")
        cid = f"room:{name}"
        need(cid not in v.convs, f"room #{name} already exists")
        members = {str(m).lstrip("@") for m in p.get("members") or []} | {act.actor}
        unknown = sorted(m for m in members if m not in v.members)
        need(not unknown, f"unknown members {unknown}")
        rule = self._policy(Check(act="create_room", actor=act.actor))
        conv = Conversation(id=cid, kind="room", members=members, topic=str(p.get("topic", "")),
                            created_by=act.actor)
        plan.add(f"conv/{cid}", "conversation_created", {"conv": conv.model_dump(mode="json")})
        plan.data = {"_rule": rule, "conv": cid}
        return plan

    def _plan_add_member(self, act: Act, p: dict, remove: bool = False) -> Plan:
        v, plan = self.view, Plan()
        cid = f"room:{str(p.get('room', '')).lstrip('#')}"
        conv = v.convs.get(cid)
        need(conv is not None, f"no room '{cid[5:]}'")
        need(act.actor in conv.members, f"you are not a member of #{cid[5:]}")
        who = str(p.get("member", "")).lstrip("@")
        need(who in v.members, f"unknown member '{who}'")
        need((who in conv.members) == remove,
              f"@{who} is {'not ' if remove else 'already '}a member of #{cid[5:]}")
        kind = "remove_member" if remove else "add_member"
        rule = self._policy(Check(act=kind, actor=act.actor, target=who, conv=conv))
        plan.add(f"conv/{cid}", "conv_member_removed" if remove else "conv_member_added",
                 {"conv": cid, "member": who})
        plan.data = {"_rule": rule, "conv": cid}
        return plan

    def _plan_remove_member(self, act: Act, p: dict) -> Plan:
        return self._plan_add_member(act, p, remove=True)

    # ---- tasks --------------------------------------------------------------------------
    def _deps(self, raw: Any) -> list[Dependency]:
        deps = []
        for d in raw or []:
            if isinstance(d, str):
                d = {"task": d}
            dep = Dependency.model_validate(d)
            t = self.view.resolve_task(dep.task)
            need(t is not None, f"unknown dependency task '{dep.task}'")
            deps.append(Dependency(task=t.id, type=dep.type))
        return deps

    def _member_arg(self, raw: Any, what: str) -> str | None:
        if raw in (None, ""):
            return None
        mid = str(raw).lstrip("@")
        need(mid in self.view.members, f"unknown {what} '{mid}'")
        return mid

    def _plan_task_create(self, act: Act, p: dict) -> Plan:
        v, plan = self.view, Plan()
        title = str(p.get("title", "")).strip()
        need(bool(title), "task needs a title")
        parent = None
        if p.get("parent"):
            pt = v.resolve_task(str(p["parent"]))
            need(pt is not None, f"unknown parent task '{p['parent']}'")
            parent = pt.id
        tid = f"T{len(v.tasks) + 1}"
        task = Task(id=tid, short=tid, title=title, description=str(p.get("description", "")),
                    parent=parent, deps=self._deps(p.get("deps")), created_by=act.actor,
                    reviewer=self._member_arg(p.get("reviewer"), "reviewer"),
                    watchers={w for w in (self._member_arg(x, "watcher")
                                          for x in p.get("watchers") or []) if w},
                    estimate_h=p.get("estimate_h"), labels=list(p.get("labels") or []),
                    acceptance=str(p["acceptance"]).strip() or None if p.get("acceptance") else None)
        rule = self._policy(Check(act="task_create", actor=act.actor, task=task))
        assignee = self._member_arg(p.get("assignee"), "assignee")
        if assignee:
            self._policy(Check(act="task_transition", transition="assign", actor=act.actor,
                               target=assignee, task=task))
        conv = Conversation(id=task.conv, kind="task", members=task.parties(),
                            topic=title, created_by=act.actor)
        plan.add(f"conv/{task.conv}", "conversation_created",
                 {"conv": conv.model_dump(mode="json")})
        plan.add(f"task/{tid}", "task_created", {"task": task.model_dump(mode="json")})
        if assignee:
            plan.add(f"task/{tid}", "task_status_changed", {
                "task_id": tid, "transition": "assign", "frm": "open", "to": "assigned",
                "assignee": assignee})
        plan.data = {"_rule": rule, "task": tid}
        return plan

    def _plan_task_transition(self, act: Act, p: dict) -> Plan:
        v, plan = self.view, Plan()
        task = v.resolve_task(str(p.get("task", "")))
        need(task is not None, f"unknown task '{p.get('task')}'")
        tr = str(p.get("transition", ""))
        to = self._member_arg(p.get("to"), "member") if tr == "assign" else None
        err = check_transition(task, tr, v.tasks, to=to, members=v.members)
        need(err is None, err or "")
        rule = self._policy(Check(act="task_transition", transition=tr, actor=act.actor,
                                  target=to, task=task))
        note = str(p.get("note") or "").strip()
        need(tr not in ("block", "reject", "reopen", "cancel") or bool(note),
              f"{tr} needs a note explaining why")
        payload: dict[str, Any] = {"task_id": task.id, "transition": tr, "frm": task.status,
                                   "to": target_state(task, tr), "note": note}
        if tr in ("assign", "claim", "unassign"):
            payload["assignee"] = to if tr == "assign" else act.actor if tr == "claim" else None
        if tr == "submit":
            payload["artifacts"] = _attachments(p.get("artifacts"))
            if p.get("commit"):
                payload["commit"] = str(p["commit"]).strip()
        plan.add(f"task/{task.id}", "task_status_changed", payload)
        if tr == "cancel":
            for child in descendants(v.tasks, task.id):
                if child.status not in TERMINAL:
                    plan.add(f"task/{child.id}", "task_status_changed", {
                        "task_id": child.id, "transition": "cancel", "frm": child.status,
                        "to": "cancelled", "note": f"parent {task.id} cancelled"})
        if note:
            n = len(v.msg_index) + 1
            msg = Message(msg_id=f"m{n}", conv=task.conv, author=act.actor,
                          text=f"[{tr}] {note}", thread=f"m{n}")
            if act.actor not in v.convs[task.conv].members:
                plan.add(f"conv/{task.conv}", "conv_member_added",
                         {"conv": task.conv, "member": act.actor})
            plan.add(f"conv/{task.conv}", "message_posted",
                     {"message": msg.model_dump(mode="json", exclude={"seq", "ts"})})
        plan.data = {"_rule": rule, "task": task.id, "status": payload["to"]}
        return plan

    def _plan_task_update(self, act: Act, p: dict) -> Plan:
        v, plan = self.view, Plan()
        task = v.resolve_task(str(p.get("task", "")))
        need(task is not None, f"unknown task '{p.get('task')}'")
        need(task.status not in TERMINAL, f"{task.id} is {task.status}")
        fields: dict[str, Any] = {}
        for k in ("title", "description", "estimate_h", "labels"):
            if p.get(k) is not None:
                fields[k] = p[k]
        if p.get("deps") is not None:
            deps = self._deps(p["deps"])
            need(all(d.task != task.id for d in deps), "a task cannot depend on itself")
            need(not would_cycle(v.tasks, task.id, deps), "these dependencies create a cycle")
            fields["deps"] = [d.model_dump() for d in deps]
        if p.get("acceptance") is not None:
            need(act.actor == task.created_by,
                 f"only the task's creator (@{task.created_by}) may change its acceptance command")
            fields["acceptance"] = str(p["acceptance"]).strip() or None
        if p.get("reviewer") is not None:
            fields["reviewer"] = self._member_arg(p["reviewer"], "reviewer")
        if p.get("watchers") is not None:
            fields["watchers"] = sorted({w for w in (self._member_arg(x, "watcher")
                                                     for x in p["watchers"]) if w})
        need(bool(fields), "nothing to update")
        rule = self._policy(Check(act="task_update", actor=act.actor, task=task))
        plan.add(f"task/{task.id}", "task_updated", {"task_id": task.id, "fields": fields})
        plan.data = {"_rule": rule, "task": task.id}
        return plan

    # ---- system operations (genesis from configuration; not agent Acts) ------------------
    def register_member(self, m: Member) -> None:
        with self._lock:
            if m.id in self.view.members:
                return
            if not ID_RE.match(m.id) or m.id == HUMAN_ALIAS:
                raise ValueError(f"invalid member id '{m.id}'")
            self._append("roster", "member_registered", {"member": m.model_dump(mode="json")},
                         SYSTEM)

    def system_room(self, name: str, members: set[str], topic: str = "") -> None:
        with self._lock:
            cid = f"room:{name}"
            if cid in self.view.convs:
                return
            conv = Conversation(id=cid, kind="room", members=members, topic=topic,
                                created_by=SYSTEM)
            self._append(f"conv/{cid}", "conversation_created",
                         {"conv": conv.model_dump(mode="json")}, SYSTEM)

    # ---- reading ------------------------------------------------------------------------
    def can_read(self, actor: str, cid: str) -> bool:
        conv = self.view.convs.get(cid)
        if conv is None:
            return False
        m = self.view.members.get(actor)
        return actor in conv.members or bool(m and m.kind == "human" and
                                             self.policy.humans_see_all)


class CommClient:
    """What a tool sees: the service bound to one actor and activation."""

    def __init__(self, service: CommService, actor: str, activation_id: str | None = None):
        self.service = service
        self.actor = actor
        self.activation_id = activation_id

    @property
    def view(self) -> CommView:
        return self.service.view

    def submit(self, kind: str, params: dict[str, Any]) -> SubmitResult:
        return self.service.submit(Act(act_id=new_id("act"), actor=self.actor, kind=kind,
                                       params=params, activation_id=self.activation_id))
