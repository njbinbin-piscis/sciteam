"""X-cog structural invariants (docs/60 §13.4) checked on a finished team run.

I1  every `verified` fact names a verifier-role member other than the task's assignee
I2  before a prover submits, none of its tool calls reads another prover's deliverable
I3  advisor and librarian never act on tasks (create, transition, update)
I4  the goal is accepted only after every sub-task is verified

Writes <run>/xcog_invariants.json and prints a summary.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.events import EventStore

READ = re.compile(r"\b(cat|less|head|tail|grep|sed|awk|diff|open|read_text|python3?\s+-)\b")


def main(run: Path) -> dict:
    store = EventStore(run / "events.db")
    try:
        evs = store.read_global()
    finally:
        store.close()
    blobs = BlobStore(run / "blobs")
    roles = {
        e.payload["member"]["id"]: e.payload["member"].get("role", "")
        for e in evs
        if e.type == "member_registered"
    }
    tasks: dict[str, dict] = {}
    for e in evs:
        if e.type == "task_created":
            t = e.payload.get("task", e.payload)
            tasks[t["id"]] = dict(t)
        elif e.type == "task_updated" and e.payload.get("task_id") in tasks:
            tasks[e.payload["task_id"]].update(e.payload.get("fields", {}))
    assign = {}
    for e in evs:
        if e.type == "task_status_changed" and e.payload.get("assignee"):
            assign[e.payload["task_id"]] = e.payload["assignee"]
    for tid, t in tasks.items():
        assign.setdefault(tid, t.get("assignee"))
    facts = [e for e in evs if e.type == "fact_established"]

    i1 = [
        {"task": f.payload["subject"], "by": f.payload["by"]}
        for f in facts
        if f.payload["fact"] == "verified"
        and (roles.get(f.payload["by"]) != "verifier" or f.payload["by"] == assign.get(f.payload["subject"]))
    ]

    provers = sorted(m for m, r in roles.items() if r == "prover")
    own: dict[str, set[str]] = {p: set() for p in provers}
    submit: dict[str, int] = {}
    for e in evs:
        if e.type == "task_status_changed" and e.payload.get("to") == "in_review" and e.actor in own:
            submit.setdefault(e.actor, e.seq)
    calls: list[tuple[int, str, str]] = []
    for e in evs:
        if e.type != "model_returned" or not e.stream.startswith("agent/"):
            continue
        agent = e.stream.split("/")[1]
        if agent not in own:
            continue
        turn = json.loads(blobs._path(e.payload["turn_ref"]).read_bytes())
        for c in turn.get("tool_calls") or []:
            calls.append((e.seq, agent, c.get("arguments_raw") or json.dumps(c.get("arguments"))))
    for tid, t in tasks.items():
        if assign.get(tid) in own:
            own[assign[tid]].update(re.findall(r"results/([\w.-]+)", t.get("acceptance") or ""))
    i2 = []
    for seq, agent, args in calls:
        if seq >= submit.get(agent, 10**12):
            continue
        for m in re.finditer(r"results/([\w.-]+)", args):
            f = m.group(1)
            if f in own[agent] or f.startswith(".") or not any(f in own[p] for p in provers if p != agent):
                continue
            if READ.search(args) or '"path"' in args:
                i2.append({"seq": seq, "prover": agent, "file": f})

    kinds = {"task_create", "task_transition", "task_update"}
    acts = {e.payload["act"]["act_id"]: e.payload["act"] for e in evs if e.type == "act_submitted"}
    i3 = [
        {"seq": e.seq, "actor": acts[e.payload["act_id"]]["actor"], "kind": e.payload["kind"]}
        for e in evs
        if e.type == "act_decided"
        and e.payload["kind"] in kinds
        and e.payload["act_id"] in acts
        and roles.get(acts[e.payload["act_id"]]["actor"]) in ("advisor", "librarian")
    ]

    verified_at = {f.payload["subject"]: f.seq for f in facts if f.payload["fact"] == "verified"}
    roots = [tid for tid, t in tasks.items() if not t.get("parent")]
    i4 = []
    for r in roots:
        acc = next(
            (f.seq for f in facts if f.payload["fact"] == "accepted" and f.payload["subject"] == r), None
        )
        kids = [tid for tid, t in tasks.items() if t.get("parent") == r]
        late = [k for k in kids if acc is None or verified_at.get(k, 10**12) > acc]
        if acc is None or late:
            i4.append({"root": r, "accepted_seq": acc, "unverified_children": late})

    denials = [
        {"seq": e.seq, "kind": e.payload["kind"], "reason": e.payload["reason"][:160]}
        for e in evs
        if e.type == "act_decided" and e.payload["decision"] == "deny"
    ]
    res = {
        "I1_violations": i1,
        "I2_violations": i2,
        "I3_violations": i3,
        "I4_violations": i4,
        "ok": not (i1 or i2 or i3 or i4),
        "facts": len(facts),
        "denials": denials,
        "prover_deliverables": {p: sorted(v) for p, v in own.items()},
    }
    (run / "xcog_invariants.json").write_text(json.dumps(res, indent=2) + "\n")
    return res


if __name__ == "__main__":
    r = main(Path(sys.argv[1]))
    print(json.dumps({k: v for k, v in r.items() if k != "denials"}, indent=2))
    print(f"denials: {len(r['denials'])}")
