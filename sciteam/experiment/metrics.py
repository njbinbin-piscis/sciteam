"""Per-run metrics from the event log, the replay audit and the grader (docs/60 §5).

Nothing here asks a model; every number is a count or a sum over recorded events."""

from typing import Any

from sciteam.kernel.events import Event

GOAL_REASONS = ("goal_met",)


def replay_ok(check: dict[str, Any]) -> bool:
    inst = check.get("I", {}).get("I1")
    if inst is not None:
        r = inst["replay"]
        equal = all(v for k, v in r.items() if k.endswith("_equal"))
        return r["decision_mismatches"] == 0 and equal and \
            inst["domain_events_without_allowed_act"] == 0
    r = check["C5"]["replay"]
    return r["decision_or_event_mismatches"] == 0 and r["domain_events_without_allowed_act"] == 0


def recovered_tasks(evs: list[Event]) -> list[str]:
    """Tasks sent back (reject or reopen) that later held the fact 'accepted'."""
    returned: set[str] = set()
    out: list[str] = []
    for e in evs:
        p = e.payload
        if e.type == "task_status_changed" and p.get("transition") in ("reject", "reopen"):
            returned.add(p["task_id"])
        elif e.type == "fact_established" and p.get("fact") == "accepted" and \
                p.get("subject") in returned and p["subject"] not in out:
            out.append(p["subject"])
    return out


def run_metrics(evs: list[Event], check: dict[str, Any], score: dict[str, Any] | None,
                owner: dict[str, int]) -> dict[str, Any]:
    stops = [e.payload.get("reason") for e in evs if e.type == "run_stopped"]
    end = stops[-1] if stops else None
    started = next((e for e in evs if e.type == "run_started"), None)
    cfg = started.payload.get("config", {}) if started else {}
    tot = check.get("totals", {})
    inst = check.get("I", {})
    org = check.get("O", {})
    transitions = check.get("C4", {}).get("transitions", {})
    has_goals = "O" in check
    return {
        "score": score["score"] if score else None,
        "passed": score.get("passed") if score else None,
        "total": score.get("total") if score else None,
        "end_reason": end,
        # closed/stuck only mean something for a run with goals (the org layer, docs/50 §6)
        "closed": (end in GOAL_REASONS) if has_goals else None,
        "breaker": bool(end) and (end.startswith("breaker") or end == "human"),
        "stuck": (end == "quiescent") if has_goals else None,
        "recovered": len(recovered_tasks(evs)),
        "returned": transitions.get("reject", 0) + transitions.get("reopen", 0),
        "cost_usd": tot.get("cost_usd"),
        "wall_s": tot.get("wall_s"),
        "activations": tot.get("activations"),
        "messages": tot.get("messages"),
        "denials_by_rule": inst.get("I5", {}).get("denials_by_rule", {}),
        "amendments": len(inst.get("I3", {}).get("amendments", [])),
        "hires": len(org.get("O2_hires", [])),
        "replay_ok": replay_ok(check),
        "owner_decisions": owner.get("decisions", 0),
        "owner_reviews": owner.get("reviews", 0),
        "owner_nudges": owner.get("nudges", 0),
        # what the provider says it ran: a rolling alias can change underneath a fixed name
        "models_seen": sorted({e.payload["model"] for e in evs
                               if e.type == "model_returned" and e.payload.get("model")}),
        "interpreter_digest": cfg.get("interpreter_digest", ""),
        "config_digest": cfg.get("digest", ""),
    }
