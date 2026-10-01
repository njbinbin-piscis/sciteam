"""Automatic C1–C6, I1–I6 and O1–O5 checks on a team run's event store (docs/31-runtime.md §12,
docs/40-institution.md, docs/50-organization.md §11).

Usage: python bench/team/check.py runs/team/<run> [--json out.json]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sciteam.comm import Act, CommService  # noqa: E402
from sciteam.comm.policy import PolicyGate  # noqa: E402
from sciteam.comm.view import COMM_EVENT_TYPES  # noqa: E402
from sciteam.institution.gate import InstitutionGate  # noqa: E402
from sciteam.institution.model import RuleSet  # noqa: E402
from sciteam.institution.service import InstitutionService  # noqa: E402
from sciteam.institution.team import InstitutionTeamRun, git_commit  # noqa: E402
from sciteam.institution.view import InstitutionView  # noqa: E402
from sciteam.kernel.clock import FakeClock  # noqa: E402
from sciteam.kernel.events import EventStore  # noqa: E402
from sciteam.org.service import OrgService  # noqa: E402
from sciteam.org.team import OrgTeamRun, open_run  # noqa: E402
from sciteam.runtime.attention import agent_of_activation_stream  # noqa: E402


def replay(evs, policy) -> dict:
    """C5 audit: every comm domain event is caused by an allowed decision, and resubmitting
    every Act, in order, to a fresh CommService reproduces each decision and its events."""
    decided = {e.seq: e for e in evs if e.type == "act_decided"}
    orphans = [e.seq for e in evs if e.type in COMM_EVENT_TYPES and e.actor != "system" and
               (e.causation_id not in decided or
                decided[e.causation_id].payload["decision"] != "allow")]
    caused: dict[int, list] = {}
    for e in evs:
        if e.causation_id in decided:
            caused.setdefault(e.causation_id, []).append(e)
    by_act = {e.payload["act_id"]: e for e in decided.values()}
    clock = FakeClock()
    with TemporaryDirectory() as tmp:
        store = EventStore(Path(tmp) / "replay.db", clock=clock)
        svc = CommService(store, policy, clock)
        mismatches, acts = [], 0
        for e in evs:
            clock._now = e.ts
            if e.type in COMM_EVENT_TYPES and e.causation_id is None:
                store.append(e.stream, e.type, e.payload, actor=e.actor)
                svc.catch_up()
            elif e.type == "act_submitted":
                acts += 1
                act = Act.model_validate(e.payload["act"])
                got = svc.submit(act)
                orig = by_act[act.act_id]
                want = [(x.stream, x.type) for x in caused.get(orig.seq, [])]
                have = [(x.stream, x.type) for x in store.read_global()
                        if x.causation_id is not None and x.seq > svc_last(store, act.act_id)]
                if got.decision.decision != orig.payload["decision"] or want != have:
                    mismatches.append({"act_id": act.act_id, "kind": act.kind,
                                       "orig": orig.payload["decision"],
                                       "replay": got.decision.decision})
        store.close()
    return {"acts": acts, "decision_or_event_mismatches": len(mismatches),
            "mismatch_examples": mismatches[:5], "domain_events_without_allowed_act":
            len(orphans)}


def svc_last(store: EventStore, act_id: str) -> int:
    for x in reversed(store.read("gate")):
        if x.type == "act_decided" and x.payload["act_id"] == act_id:
            return x.seq
    raise KeyError(act_id)


def outcomes_by_act(evs) -> dict[str, list[str]]:
    """act id -> [first decision, resolution...]."""
    out: dict[str, list[str]] = {}
    for e in evs:
        if e.type in ("act_decided", "act_resolved"):
            out.setdefault(e.payload["act_id"], []).append(e.payload["decision"])
    return out


def inst_replay(tr: InstitutionTeamRun, evs) -> dict:
    """I1/I3 audit: rebuild the run from its external inputs only (genesis, the initial rule
    set, validation results) by resubmitting every Act in order to a fresh institution, settling
    after each event; compare every decision, the facts, the ledger and the rule versions."""
    clock = FakeClock()
    first = next(e for e in evs if e.type == "rule_version_activated")
    rs0 = RuleSet(tr.team.institution, tr.team.comm_policy, first.payload["texts"])
    is_org = isinstance(tr, OrgTeamRun)
    with TemporaryDirectory() as tmp:
        store = EventStore(Path(tmp) / "replay.db", clock=clock)
        inst = InstitutionView(tr.team.institution, tr.team.comm_policy)
        gate = InstitutionGate(inst, lambda ref: git_commit(tr.workspace, ref))
        comm = CommService(store, rs0.comm, clock, extra_gate=gate)
        svc = InstitutionService(comm, inst)
        org = OrgService(comm, svc, tr.configs, tr.team.org) if is_org else None

        def settle() -> None:
            svc.settle()
            if org is not None:
                org.settle()
        missing_validations = 0
        # Results are matched by (act, validator), not by id, so a replay that requests a
        # different set of validations (e.g. after a gate fix) still lines up the rest.
        asked = {e.payload["validation_id"]: (e.payload["act_id"], e.payload["validator"])
                 for e in evs if e.type == "validation_requested"}
        for e in evs:
            clock._now = e.ts
            if (e.type in COMM_EVENT_TYPES and e.causation_id is None) or \
                    (e.type == "rule_version_activated" and e.payload.get("amendment") is None):
                store.append(e.stream, e.type, e.payload, actor=e.actor)
                comm.catch_up()
            elif e.type == "act_submitted":
                comm.submit(Act.model_validate(e.payload["act"]))
            elif e.type == "validation_finished":
                p = e.payload
                vid = next((v.id for v in inst.validations.values() if v.status == "requested"
                            and (v.act_id, v.validator) == asked.get(p["validation_id"])), None)
                if vid is None:
                    missing_validations += 1
                    continue
                svc.record_validation(vid, ok=p["ok"], exit_code=p["exit_code"],
                                      wall_s=p.get("wall_s", 0), output_blob=p["output_blob"],
                                      tail=p.get("tail", ""), cached_from=p.get("cached_from"),
                                      infra=p.get("infra", False))
            else:
                continue
            settle()
        clock._now = evs[-1].ts
        settle()
        got = outcomes_by_act(store.read_global())
        want = outcomes_by_act(evs)
        diff = [{"act_id": a, "orig": want[a], "replay": got.get(a)} for a in want
                if got.get(a) != want[a]]
        orig = tr.inst
        result = {
            "acts": len(want), "decision_mismatches": len(diff), "examples": diff[:5],
            "validations_not_requested_in_replay": missing_validations,
            "facts_equal": inst.facts.keys() == orig.facts.keys(),
            "ledger_equal": {k: round(v, 6) for k, v in inst.totals.items()} ==
            {k: round(v, 6) for k, v in orig.totals.items()},
            "versions_equal": [r.version for _, r in inst.versions] ==
            [r.version for _, r in orig.versions],
        }
        if org is not None:
            def org_trail(events):
                return [(e.type, e.payload.get("manifest_sha") or e.payload.get("goals"))
                        for e in events if e.type in ("org_enacted", "goal_met", "goal_reopened")]
            result["org_equal"] = org_trail(store.read_global()) == org_trail(evs)
            result["members_equal"] = sorted(comm.view.members) == sorted(tr.comm.view.members)
        store.close()
    return result


def inst_check(tr: InstitutionTeamRun, evs) -> dict:
    inst, view = tr.inst, tr.comm.view
    allowed = {e.seq for e in evs if e.type in ("act_decided", "act_resolved") and
               e.payload["decision"] == "allow"}
    orphans = [e.seq for e in evs if e.type in COMM_EVENT_TYPES and e.actor != "system" and
               e.causation_id not in allowed]
    done = [t for t in view.tasks.values() if t.status == "done"]

    def validated(tid: str) -> bool:
        act = inst.last_submit.get(tid)
        vs = inst.validations_for(act) if act else {}
        return bool(vs) and all(v.status == "passed" for v in vs.values())

    accepted = {s for f, s in inst.facts if f == "accepted"}
    failed = [v for v in inst.validations.values() if v.status == "failed"]
    red = [x for x in inst.ledger if x["rule"] == "red-build"]
    denials = Counter(e.payload.get("rule") for e in evs
                      if e.type in ("act_decided", "act_resolved") and
                      e.payload["decision"] == "deny")
    releases = []
    for t in view.tasks.values():
        if "release" not in t.labels:
            continue
        approvals = [e for e in evs if e.type == "approval_decided"]
        done_seq = next((h.seq for h in t.history if h.to == "done"), None)
        requested = [e for e in evs if e.type == "approval_requested" and
                     inst.acts.get(e.payload["key"]) is not None and
                     inst.acts[e.payload["key"]].params.get("task") in (t.id, t.short)]
        releases.append({"task": t.id, "status": t.status, "done_seq": done_seq,
                         "signoff_requested": len(requested),
                         "decided": [(e.seq, e.payload["decision"], e.payload["by"])
                                     for e in approvals],
                         "done_before_signoff": done_seq is not None and
                         not any(e.seq < done_seq and e.payload["decision"] == "approve"
                                 for e in approvals)})
    return {
        "I1": {"domain_events_without_allowed_act": len(orphans),
               "replay": inst_replay(tr, evs)},
        "I2": {"done_tasks": len(done),
               "done_without_passed_validation": [t.id for t in done if not validated(t.id)],
               "accepted": sorted(accepted),
               "done_not_accepted": sorted(t.id for t in done if t.id not in accepted),
               "accepted_not_done": sorted(a for a in accepted
                                           if view.tasks[a].status != "done")},
        "I3": {"versions": [{"seq": s, "version": r.version} for s, r in inst.versions],
               "amendments": [{"id": a.id, "by": a.proposer, "path": a.path,
                               "status": a.status, "reason": a.reason, "votes": a.votes,
                               "rationale": a.rationale[:300]}
                              for a in inst.amendments.values()]},
        "I4": {"releases": releases},
        "I5": {"validations": len(inst.validations),
               "failed": len(failed), "passed": sum(1 for v in inst.validations.values()
                                                    if v.status == "passed"),
               "cached": sum(1 for e in evs if e.type == "validation_finished" and
                             e.payload.get("cached_from")),
               "infra": sum(1 for e in evs if e.type == "validation_finished" and
                            e.payload.get("infra")),
               "denials_by_rule": dict(denials), "pending_at_end": len(inst.pending)},
        "I6": {"balances": dict(inst.totals), "entries": Counter(x["rule"] for x in inst.ledger),
               "failed_validations_debited": len(red)},
    }


def org_check(tr: OrgTeamRun, evs) -> dict:
    """O1–O5: how the organization came about and what it did (docs/50 §12)."""
    org, inst = tr.org, tr.inst
    decided = {e.payload["act_id"]: e.payload for e in evs
               if e.type in ("act_decided", "act_resolved")}
    by_kind: dict[str, Counter] = {}
    for e in evs:
        if e.type == "act_submitted":
            k = e.payload["act"]["kind"]
            if k in ("hire", "dismiss", "certify_environment", "revoke_environment",
                     "propose_amendment"):
                d = decided.get(e.payload["act"]["act_id"], {})
                by_kind.setdefault(k, Counter())[d.get("decision", "?")] += 1
    hires = [{"member": e.payload["member"]["id"], "seat": e.payload["seat"],
              "by": e.payload["hired_by"], "template": e.payload["template"],
              "adapted": e.payload["adapted"], "seq": e.seq}
             for e in evs if e.type == "member_registered" and e.payload.get("seat")]
    amendments = [{"id": a.id, "path": a.path, "proposer": a.proposer, "status": a.status,
                   "reason": a.reason, "rationale": a.rationale[:160]}
                  for a in inst.amendments.values()]
    enacted = [{"seq": e.seq, "rule_version": e.payload["rule_version"],
                "vacancies": e.payload["vacancies"]} for e in evs if e.type == "org_enacted"]
    certs = [{"env": e.payload["env"], "by": e.payload["by"], "seq": e.seq}
             for e in evs if e.type == "environment_certified"]
    starts = [e for e in evs if e.type == "task_status_changed"
              and e.payload["transition"] == "start"]
    first_cert = certs[0]["seq"] if certs else None
    goals = [{"task": t.id, "title": t.title, "status": t.status, "predicate": t.acceptance,
              "met": inst.has_fact(org.manifest.goals.fact, t.id) if org.manifest.goals else None}
             for t in org.goals(include_cancelled=True)]
    return {
        "acts": {k: dict(v) for k, v in by_kind.items()},
        "O1_enacted": enacted, "amendments": amendments,
        "O2_hires": hires, "seats": {s.name: {"count": s.count, "filled": org.seated(s.name)}
                                     for s in org.manifest.seats},
        "O3_certificates": certs,
        "O3_starts_before_certificate": sum(1 for e in starts
                                            if first_cert is None or e.seq < first_cert),
        "O4_goals": goals, "goal_met": [e.seq for e in evs if e.type == "goal_met"],
        "policy": tr.comm.policy.name,
    }


def check(run_dir: Path) -> dict:
    tr = open_run(run_dir, models={})
    try:
        evs = tr.store.read_global()
        view = tr.comm.view
        policy = tr.comm.policy
        gate = PolicyGate(policy, tr.clock)
        msgs = [e.payload["message"] for e in evs if e.type == "message_posted"]
        decided = [e.payload for e in evs if e.type == "act_decided"]
        denials = [d for d in decided if d["decision"] != "allow"]
        starts = [e for e in evs if e.type == "activation_started"]
        finishes = [e for e in evs if e.type == "activation_finished"]
        agents = {m.id for m in view.agents()}

        # C1: private conversations, and no non-member ever wrote into one.
        dm_msgs = [m for m in msgs if m["conv"].startswith("dm:")]
        dm_leak = [m for m in dm_msgs if m["author"] not in view.convs[m["conv"]].members]
        # C2: room messages and thread replies.
        room_msgs = [m for m in msgs if m["conv"].startswith(("room:", "task:"))]
        replies = [m for m in msgs if m.get("reply_to")]
        # C3: every activation was caused by delivered attention (or resumed), and every
        # mentioned agent was activated after the mention.
        unexplained = [e.stream for e in starts if not (e.payload.get("meta") or {}).get("attention")]
        mentions = [(e.seq, e.ts, mid) for e in evs if e.type == "message_posted"
                    for mid in e.payload["message"]["mentions"] if mid in agents]
        delivered: dict[tuple[str, int], object] = {}   # (agent, seq) -> first delivery ts
        for e in evs:
            if e.type in ("activation_started", "incoming_added"):
                a = agent_of_activation_stream(e.stream)
                for s in (e.payload.get("meta") or {}).get("attention", []):
                    delivered.setdefault((a, s), e.ts)
        lat = sorted((delivered[(a, s)] - ts).total_seconds() for s, ts, a in mentions
                     if (a, s) in delivered)
        unanswered = [(s, a) for s, _, a in mentions if (a, s) not in delivered]
        # Non-waking chatter a member never saw because nothing woke them afterwards.
        undelivered_room = sum(1 for e in evs if e.type == "message_posted"
                               for a in agents - {e.payload["message"]["author"]}
                               if a in view.convs[e.payload["message"]["conv"]].members
                               and (a, e.seq) not in delivered)
        # C4: tasks moved through the state machine; illegal transitions were refused.
        transitions = Counter(e.payload["transition"] for e in evs
                              if e.type == "task_status_changed")
        statuses = Counter(t.status for t in view.tasks.values())
        structural_denials = [d for d in denials if d.get("rule") == "structure"]
        # C5: every allowed act is allowed by the policy file; list the communication graph.
        graph = Counter()
        for m in dm_msgs:
            others = sorted(view.convs[m["conv"]].members - {m["author"]})
            graph[f"{m['author']}->{others[0]}"] += 1
        policy_denials = [d for d in denials if d.get("rule") not in ("structure", None)
                          or "no rule allows" in d.get("reason", "")]
        # C6: how the run ended.
        ends = [e.payload for e in evs if e.type in ("run_quiescent", "run_stopped",
                                                     "breaker_tripped")]
        cost = sum(float((e.payload.get("totals") or {}).get("cost_usd", 0)) for e in finishes)
        outcomes = Counter(e.payload["outcome"] for e in finishes)
        wall = (evs[-1].ts - evs[0].ts).total_seconds() if evs else 0
        return {
            "run": run_dir.name, "policy": policy.name,
            "C1": {"dm_messages": len(dm_msgs), "dm_conversations": len({m["conv"] for m in dm_msgs}),
                   "non_member_writes": len(dm_leak)},
            "C2": {"room_or_task_messages": len(room_msgs), "thread_replies": len(replies),
                   "rooms": sorted(c for c in view.convs if c.startswith("room:"))},
            "C3": {"activations": len(starts), "without_attention": len(unexplained),
                   "agent_mentions": len(mentions), "mentions_never_delivered": len(unanswered),
                   "mention_delivery_s": {
                       "p50": lat[len(lat) // 2] if lat else None,
                       "max": lat[-1] if lat else None},
                   "member_messages_never_delivered": undelivered_room},
            "C4": {"tasks": len(view.tasks), "statuses": dict(statuses),
                   "transitions": dict(transitions),
                   "structural_denials": len(structural_denials)},
            "C5": {"dm_graph": dict(graph), "denials": len(denials),
                   "policy_denials": len(policy_denials),
                   "denial_examples": [d["reason"] for d in denials[:5]],
                   "replay": "see I1" if isinstance(tr, InstitutionTeamRun)
                   else replay(evs, policy)},
            "C6": {"end": ends[-1] if ends else None,
                   "breakers": [e for e in ends if "kind" in e]},
            "totals": {"cost_usd": round(cost, 4), "wall_s": round(wall), "messages": len(msgs),
                       "activations": len(starts), "outcomes": dict(outcomes),
                       "per_agent_activations": dict(Counter(
                           agent_of_activation_stream(e.stream) for e in starts))},
            "_gate_rules": len(gate.policy.rules),
            **({"I": inst_check(tr, evs)} if isinstance(tr, InstitutionTeamRun) else {}),
            **({"O": org_check(tr, evs)} if isinstance(tr, OrgTeamRun) else {}),
        }
    finally:
        tr.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    out = check(a.run_dir)
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    print(text)
    if a.json:
        a.json.write_text(text)


if __name__ == "__main__":
    main()
