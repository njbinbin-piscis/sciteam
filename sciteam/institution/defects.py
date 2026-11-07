"""Defect records (docs/09 §5.1): accepted results later shown wrong. Each record names what
the original decision rested on — the rules it satisfied, its endorsers and their harnesses, the
rule version — so an amendment can cite it and re-adjudication can find the Acts it concerns."""

from collections import Counter
from collections.abc import Iterable
from typing import Any

from sciteam.comm.model import Act
from sciteam.comm.view import CommView
from sciteam.institution.gate import matches
from sciteam.institution.view import InstitutionView
from sciteam.kernel.events import Event

DEFECT_SOURCES = ("human_overruled", "acceptance_rejected", "replication_failed",
                  "claim_overturned")
HARNESS_EVENTS = ("budget_paused", "breaker_tripped")


def decision_record(inst: InstitutionView, view: CommView, task_id: str,
                    claim_id: str | None = None) -> dict[str, Any]:
    """What made `task_id` done: the approving Act, the rules that applied to it under the rule
    version it was decided by, and who endorsed it (approver, attesters of its claims)."""
    act_id = inst.done_by.get(task_id)
    act = inst.acts.get(act_id) if act_id else None
    rs = inst.ruleset_at(inst.decided_seq.get(act_id, 0)) if act_id else None
    rules = []
    if act is not None and rs is not None:
        rules = [r.name for r in (*rs.rules.requirements, *rs.rules.approvals)
                 if matches(r, act, view)]
    endorsers = []
    if act is not None:
        endorsers.append(_endorser(view, act.actor, "approved"))
    for a in inst.attestations.values():
        if a.task_id == task_id and a.verdict == "pass" and (claim_id is None or
                                                             a.claim_id == claim_id):
            endorsers.append(_endorser(view, a.by, f"attested {a.name} ({a.claim_id})"))
    return {"act_id": act_id, "rule_version": act.rule_version if act else None,
            "rules": rules, "endorsers": endorsers,
            "fact": "accepted" if inst.has_fact("accepted", task_id) else None}


def _endorser(view: CommView, member: str, did: str) -> dict[str, Any]:
    m = view.members.get(member)
    return {"member": member, "did": did, "role": m.role if m else "",
            "harness": m.harness if m else None, "profile": m.profile if m else ""}


def defect_acts(inst: InstitutionView, defect: dict[str, Any]) -> set[str]:
    """The allowed Acts a defect concerns: the transitions that submitted and approved its task,
    and the pass attestations of its claim (all claims of the task when none is named)."""
    task, claim = defect["task_id"], defect.get("claim_id")
    want = inst.claims[claim].name if claim in inst.claims else claim
    out = set()
    for aid, act in inst.acts.items():
        p = act.params
        if _task_of(inst, act) != task:
            continue
        if act.kind == "task_transition" and p.get("transition") in ("submit", "approve"):
            out.add(aid)
        elif act.kind == "attest" and p.get("verdict") == "pass":
            ref = str(p.get("claim", "")).strip()
            c = inst.claims.get(ref.upper())
            if want is None or (c.name if c else ref) == want:
                out.add(aid)
    return out


def _task_of(inst: InstitutionView, act: Act) -> str | None:
    t = inst.comm.resolve_task(str(act.params.get("task", "")))
    return t.id if t else None


def harness_defects(events: Iterable[Event]) -> dict[str, dict[str, int]]:
    """Budget and breaker pauses per member: defects of the harness, not of the rules."""
    out: dict[str, Counter] = {}
    for ev in events:
        if ev.type in HARNESS_EVENTS:
            who = ev.payload.get("agent") or ev.payload.get("member") or ev.actor or "run"
            out.setdefault(who, Counter())[ev.type] += 1
    return {m: dict(c) for m, c in sorted(out.items())}


def render_defects(inst: InstitutionView, limit: int = 20) -> str:
    if not inst.defects:
        return "No defect records."
    lines = []
    for d in list(inst.defects.values())[-limit:]:
        dec = d.get("decision") or {}
        who = ", ".join(f"@{e['member']} {e['did']}" for e in dec.get("endorsers", [])) or "-"
        lines.append(f"- {d['defect_id']} [{d['source']}] {d['task_id']}"
                     + (f" claim {d['claim_id']}" if d.get("claim_id") else "")
                     + f" by @{d['by']}: {d['reason'][:400]}\n  decided under rule version "
                     f"{dec.get('rule_version') or '-'} by rules "
                     f"{', '.join(dec.get('rules', [])) or '-'}; endorsed by {who}")
    return "\n".join(lines)
