"""Counts-as and ledger derivation (docs/40-institution.md §8). Pure functions of the event and
the fold at that event: which facts and ledger entries the rules in force derive from it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sciteam.comm.model import Task
from sciteam.comm.policy import SelCtx, compile_selector
from sciteam.comm.view import CommView
from sciteam.institution.model import Condition, EventMatch, RuleSet

if TYPE_CHECKING:
    from sciteam.institution.view import InstitutionView


@dataclass
class Owed:
    """A fact or ledger entry the rules derive from event `cause`, not yet recorded."""
    cause: int
    type: str
    payload: dict[str, Any]


def event_matches(m: EventMatch, type: str, payload: dict) -> bool:
    return m.event == type and \
        (m.transition is None or payload.get("transition") in m.transition.split("|")) and \
        (m.to is None or payload.get("to") in m.to.split("|"))


def member_of(ref: str, task: Task | None, actor: str | None) -> str | None:
    if ref in ("act.actor", "actor"):
        return actor
    if task is None:
        return None
    return {"task.assignee": task.assignee, "task.reviewer": task.reviewer,
            "task.creator": task.created_by}[ref]


def children_lacking(task: Task, fact: str, view: CommView, inst: "InstitutionView") -> list[str]:
    return [k.id for k in view.tasks.values() if k.parent == task.id and
            k.status != "cancelled" and not inst.has_fact(fact, k.id)]


def fact_holds(c: Condition, task: Task, actor: str | None, inst: "InstitutionView") -> bool:
    k = c.key
    if k == "validated":
        act_id = inst.last_submit.get(task.id)
        v = inst.validations_for(act_id).get(c.validated) if act_id else None
        return v is not None and v.status == "passed"
    if k == "approved_by":
        return actor is not None and compile_selector(c.approved_by)(
            actor, SelCtx(view=inst.comm, actor=actor, task=task))
    if k == "children_have":
        return not children_lacking(task, c.children_have, inst.comm, inst)
    if k == "field":
        return getattr(task, c.field, None) not in (None, "", [])
    if k == "distinct":
        a, b = (member_of(r, task, actor) for r in c.distinct)
        return a is not None and b is not None and a != b
    return False


def _task_of_event(type: str, p: dict, inst: "InstitutionView") -> tuple[Task | None, str | None]:
    """(subject task, the actor the event is attributed to) for events rules can react to."""
    if type == "task_status_changed":
        return inst.comm.tasks.get(p["task_id"]), None
    if type == "validation_finished":
        v = inst.validations.get(p["validation_id"])
        act = inst.acts.get(v.act_id) if v else None
        if act is None:
            return None, None
        return inst.comm.resolve_task(str(act.params.get("task", ""))), act.actor
    if type == "fact_established":
        return inst.comm.tasks.get(p["subject"]), p.get("by")
    return None, None


def derive(ev: Any, rs: RuleSet, inst: "InstitutionView") -> list[tuple[tuple, Owed]]:
    out: list[tuple[tuple, Owed]] = []
    p, rules = ev.payload, rs.rules
    task, actor = _task_of_event(ev.type, p, inst)
    if ev.type == "task_status_changed":
        actor = ev.actor
    if task is not None and ev.type == "task_status_changed":
        for f in rules.facts:
            if event_matches(f.when, ev.type, p) and not inst.has_fact(f.name, task.id) and \
                    all(fact_holds(c, task, actor, inst) for c in f.require):
                out.append((("fact", f.name, ev.seq), Owed(ev.seq, "fact_established", {
                    "fact": f.name, "subject": task.id, "rule": f.name, "by": actor,
                    "basis": [ev.seq]})))
            if f.revoke_when is not None and event_matches(f.revoke_when, ev.type, p) and \
                    inst.has_fact(f.name, task.id):
                out.append((("revoke", f.name, ev.seq), Owed(ev.seq, "fact_revoked", {
                    "fact": f.name, "subject": task.id, "rule": f.name, "basis": [ev.seq]})))
    for lr in rules.ledger:
        on = lr.when
        hit = (on.transition is not None and ev.type == "task_status_changed" and
               p.get("transition") == on.transition) or \
              (on.validation is not None and ev.type == "validation_finished" and
               not p.get("infra") and bool(p.get("ok")) == (on.validation == "passed")) or \
              (on.fact is not None and ev.type == "fact_established" and p["fact"] == on.fact)
        if not hit:
            continue
        member = member_of(lr.to, task, actor)
        if member is None:
            continue
        out.append((("ledger", lr.name, ev.seq, member), Owed(ev.seq, "ledger_entry", {
            "member": member, "amount": lr.amount, "rule": lr.name,
            "task": task.id if task else None, "basis": [ev.seq]})))
    return out
