"""Structured amendments and historical re-adjudication (docs/09 §5.2).

A structured amendment is a list of changes applied to the rules file in force. Re-adjudication
refolds the project's recorded events and decides every gated Act again, at the point it was
finally decided, under the proposed rule set: the Gate is a pure function of the Act and the
folded state, so no model runs. The recorded history (validations, approvals, facts) stays as it
was; only the rules the Gate applies change. A validator the new rules run on an Act whose commit
it never ran on is a trial: the proposal records it as a validation request, and re-adjudication
uses its result once the validation worker has run it on the recorded commit."""

from collections.abc import Callable, Iterable
from typing import Any

import yaml

from sciteam.comm.model import Act, Decision
from sciteam.institution.gate import ACCEPTANCE, InstitutionGate, act_task
from sciteam.institution.model import Condition, RuleSet
from sciteam.institution.view import InstitutionView, Validation
from sciteam.kernel.events import Event

TrialKey = tuple[str, str, str]           # (validator, command, commit)

SECTIONS = ("requirements", "approvals", "facts", "ledger")
OPS = ("add_rule", "replace_rule", "remove_rule", "add_condition", "remove_condition",
       "set_validator", "set")
CHANGES_HELP = (
    "changes: a list of edits to the rules file in force, each one of\n"
    "  {op: add_rule, section: requirements|approvals|facts|ledger, rule: {name, ...}}\n"
    "  {op: replace_rule, rule: {name, ...}}          (the rule with that name, whole)\n"
    "  {op: remove_rule, name: <rule>}\n"
    "  {op: add_condition, name: <rule>, condition: {<key>: <value>}}\n"
    "  {op: remove_condition, name: <rule>, key: <condition key>}\n"
    "  {op: set_validator, validator: <name>, command: <shell>, timeout_s?: <s>}\n"
    "  {op: set, key: rework_rounds, value: <n>}")


def apply_changes(text: str, changes: list[dict[str, Any]]) -> str:
    """The rules file after `changes`; raises ValueError naming the first change that fails."""
    data = yaml.safe_load(text) or {}
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a non-empty list")
    for i, ch in enumerate(changes, 1):
        if not isinstance(ch, dict) or ch.get("op") not in OPS:
            raise ValueError(f"change {i}: op must be one of {', '.join(OPS)}")
        try:
            _apply(data, ch)
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"change {i} ({ch['op']}): {e}") from None
    head = "".join(line + "\n" for line in text.splitlines() if line.startswith("#"))
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None,
                          width=100)
    return head + body


def _find(data: dict, name: str) -> tuple[str, int]:
    for sec in SECTIONS:
        for i, r in enumerate(data.get(sec) or []):
            if r.get("name") == name:
                return sec, i
    raise ValueError(f"no rule named '{name}'")


def _apply(data: dict, ch: dict) -> None:
    op = ch["op"]
    if op == "add_rule":
        sec, rule = ch["section"], dict(ch["rule"])
        if sec not in SECTIONS:
            raise ValueError(f"section must be one of {', '.join(SECTIONS)}")
        if any(r.get("name") == rule.get("name") for s in SECTIONS for r in data.get(s) or []):
            raise ValueError(f"a rule named '{rule.get('name')}' exists; use replace_rule")
        data.setdefault(sec, []).append(rule)
    elif op == "replace_rule":
        rule = dict(ch["rule"])
        sec, i = _find(data, rule["name"])
        data[sec][i] = rule
    elif op == "remove_rule":
        sec, i = _find(data, ch["name"])
        del data[sec][i]
    elif op == "add_condition":
        sec, i = _find(data, ch["name"])
        cond = dict(ch["condition"])
        data[sec][i].setdefault("require", []).append(cond)
    elif op == "remove_condition":
        sec, i = _find(data, ch["name"])
        req = data[sec][i].get("require") or []
        kept = [c for c in req if ch["key"] not in c]
        if len(kept) == len(req):
            raise ValueError(f"rule '{ch['name']}' has no '{ch['key']}' condition")
        data[sec][i]["require"] = kept
    elif op == "set_validator":
        spec = {"command": str(ch["command"])}
        if ch.get("timeout_s") is not None:
            spec["timeout_s"] = float(ch["timeout_s"])
        data.setdefault("validators", {})[str(ch["validator"])] = spec
    elif op == "set":
        if ch["key"] != "rework_rounds":
            raise ValueError("set takes key: rework_rounds")
        data["rework_rounds"] = int(ch["value"])


# ---- re-adjudication ----------------------------------------------------------------------

def outcome(d: Decision) -> str:
    if d.decision in ("allow", "deny"):
        return d.decision
    return "held" if any(w.startswith("approval:") for w in d.waiting) else "unverified"


def finished_runs(validations: Iterable[Validation]) -> dict[TrialKey, Validation]:
    """Finished validator runs by (validator, command, commit): what a trial can reuse."""
    return {(v.validator, v.command, v.commit): v for v in validations
            if v.status in ("passed", "failed")}


def readjudicate(events: Iterable[Event], new: RuleSet, *, constitution_path: str | None,
                 comm_path: str, resolve_commit: Callable[[str], str | None],
                 gated: Callable[[str], bool], defect_acts: set[str],
                 runs: dict[TrialKey, Validation] | None = None) -> dict[str, Any]:
    """Every gated Act of the history decided again under `new`. fixed: an allowed Act a defect
    concerns is now denied or held for approval; regressed: another allowed Act is no longer
    allowed; loosened: an Act the Gate denied is now allowed; unverified: it waits for a
    validator with no finished run on its commit, listed in `trials`."""
    view = InstitutionView(constitution_path, comm_path)
    gate = InstitutionGate(view, resolve_commit)
    runs = runs or {}
    rows: list[dict[str, Any]] = []
    trials: dict[TrialKey, dict[str, Any]] = {}
    evaluated = allowed = 0
    for ev in events:
        p = ev.payload
        final = (ev.type == "act_decided" and p.get("decision") != "pending") or \
            ev.type == "act_resolved"
        act = view.acts.get(p.get("act_id", "")) if final else None
        if act is not None and gated(act.kind) and view.rules is not None:
            was, rule = p["decision"], p.get("rule")
            if was == "allow" or rule in _gate_rules(view.rules):
                evaluated += 1
                allowed += was == "allow"
                d = _decide_under(view, gate, act, new, ev.seq, runs)
                now = outcome(d)
                if now == "unverified" and was == "allow":
                    for t in _trials(view, gate, _pinned(view, act), new, d):
                        trials.setdefault((t["validator"], t["command"], t["commit"]), t)
                row = _classify(act, view, was, now, d, act.act_id in defect_acts)
                if row is not None:
                    rows.append(row)
        view.apply(ev)
    count = {k: sum(1 for r in rows if r["class"] == k)
             for k in ("fixed", "regressed", "loosened", "unverified")}
    return {"rule_version": new.version, "evaluated": evaluated, "allowed": allowed,
            "defect_acts": len(defect_acts), **count, "changes": rows[:200],
            "trials": list(trials.values()), "never_pass": never_pass(new, runs)}


def never_pass(new: RuleSet, runs: dict[TrialKey, Validation]) -> list[str]:
    """Validators of `new` that ran on more than one commit and failed on every one: a
    validator that rejects all work rejects the defect's work for no reason of its own."""
    seen: dict[str, list[str]] = {}
    for (name, command, _commit), v in runs.items():
        if name in new.rules.validators and v.status in ("passed", "failed"):
            seen.setdefault(name, []).append(v.status)
    return sorted(n for n, st in seen.items() if len(st) > 1 and "passed" not in st)


def _gate_rules(rs: RuleSet) -> set[str]:
    return set(rs.rules.named()) | {"pending", "environments"}


def _command(new: RuleSet, name: str, task) -> str:
    cmd = new.rules.validators[name].command
    return cmd.replace(ACCEPTANCE, task.acceptance or "") if task else cmd


def _commit(gate: InstitutionGate, act: Act) -> str | None:
    ref = act.params.get("commit")
    return gate.resolve_commit(str(ref)) if ref else None


def _pinned(view: InstitutionView, act: Act) -> Act:
    """An Act recorded with a symbolic ref (HEAD) and decided with validators, as naming the
    commit those validators ran on: the ref resolves elsewhere today."""
    ref = str(act.params.get("commit") or "")
    if not ref or all(c in "0123456789abcdef" for c in ref.lower()):
        return act
    ran = next((v.commit for v in view.validations.values() if v.act_id == act.act_id), None)
    return act.model_copy(update={"params": {**act.params, "commit": ran}}) if ran else act


def _decide_under(view: InstitutionView, gate: InstitutionGate, act: Act, new: RuleSet,
                  seq: int, runs: dict[TrialKey, Validation]) -> Decision:
    """The Gate's decision on `act` under `new`, seeing only validator runs whose command is the
    one `new` runs: the Act's own, or a finished run of that command on the same commit."""
    act = _pinned(view, act)
    task = act_task(act, view.comm)
    commit = _commit(gate, act)
    stale = {vid: v for vid, v in view.validations.items() if v.act_id == act.act_id and
             v.validator in new.rules.validators and
             _command(new, v.validator, task) != v.command}
    saved = view.versions
    for vid in stale:
        del view.validations[vid]
    own = {v.validator for v in view.validations.values() if v.act_id == act.act_id}
    borrowed = {}
    if commit:
        for name in new.rules.validators:
            hit = runs.get((name, _command(new, name, task), commit))
            if name not in own and hit is not None:
                borrowed[f"~{act.act_id}:{name}"] = Validation(
                    id=f"~{act.act_id}:{name}", validator=name, commit=commit,
                    command=hit.command, act_id=act.act_id, timeout_s=hit.timeout_s,
                    status=hit.status, exit_code=hit.exit_code, tail=hit.tail)
    view.validations.update(borrowed)
    view.versions = [*saved, (seq, new)]
    try:
        return gate.decide(act, view.comm)
    except Exception as e:     # a historical Act the new rules cannot evaluate
        return Decision(decision="pending", reason=f"cannot evaluate: {e}"[:300], waiting=[])
    finally:
        view.versions = saved
        for vid in borrowed:
            del view.validations[vid]
        view.validations.update(stale)


def _trials(view: InstitutionView, gate: InstitutionGate, act: Act, new: RuleSet,
            d: Decision) -> list[dict[str, Any]]:
    task = act_task(act, view.comm)
    commit = _commit(gate, act)
    if not commit:
        return []
    return [{"validator": n, "command": _command(new, n, task), "commit": commit,
             "timeout_s": new.rules.validators[n].timeout_s}
            for w in d.waiting if w.startswith("validate:")
            for n in [w.split(":", 1)[1]] if n in new.rules.validators]


def _classify(act: Act, view: InstitutionView, was: str, now: str, d: Decision,
              defect: bool) -> dict[str, Any] | None:
    if was == "allow" and now == "allow" or was == "deny" and now != "allow":
        return None
    if was == "allow":
        cls = ("fixed" if now in ("deny", "held") else "unverified") if defect else "regressed"
    else:
        cls = "loosened"
    task = act_task(act, view.comm)
    return {"act_id": act.act_id, "kind": act.kind, "actor": act.actor,
            "task": task.id if task else act.params.get("task"),
            "transition": act.params.get("transition"), "was": was, "now": now,
            "rule": d.rule, "reason": (d.reason or "")[:400], "defect": defect, "class": cls}


def admits(review: dict[str, Any], admission) -> str | None:
    """None if the re-adjudication meets the constitution's admission thresholds, else why."""
    limit = admission.regressed_limit(review["allowed"])
    if review.get("never_pass"):
        return (f"validator {', '.join(review['never_pass'])} failed on every commit it ran on, "
                f"so its denials say nothing about the defect: fix the validator until it "
                f"passes correct work (see its output in the trial runs)")
    if review["fixed"] < admission.fixed_at_least:
        return (f"re-adjudication fixes {review['fixed']} of the {review['defect_acts']} Acts "
                f"the cited defects concern (needs >= {admission.fixed_at_least})")
    if review["regressed"] > limit:
        return (f"re-adjudication regresses {review['regressed']} of {review['allowed']} allowed "
                f"Acts (at most {limit})")
    return None


def idle_validators(old: RuleSet, new: RuleSet) -> list[str]:
    """Validators `new` defines or redefines that none of its rules runs."""
    r = new.rules
    used = {n for env in r.environments.values() for n in env.probes}
    for rule in [*r.requirements, *r.facts]:
        for c in rule.require:
            used |= set(c.validate_ or ()) | ({c.validated} if c.validated else set())
    before = old.rules.validators
    return sorted(n for n, v in r.validators.items()
                  if n not in used and (n not in before or before[n] != v))


def condition_keys() -> str:
    return ", ".join(f.alias or n for n, f in Condition.model_fields.items())


def render_review(review: dict[str, Any], limit: int = 12) -> str:
    lines = [f"re-adjudicated {review['evaluated']} recorded Acts under rule version "
             f"{review['rule_version']}: fixed {review['fixed']}, regressed "
             f"{review['regressed']}, loosened {review['loosened']}, unverified "
             f"{review['unverified']}"]
    for r in review["changes"][:limit]:
        lines.append(f"- {r['class']}: {r['kind']} {r.get('transition') or ''} {r['task'] or ''} "
                     f"by @{r['actor']} ({r['act_id']}): {r['was']} -> {r['now']}"
                     + (f" [{r['rule']}: {r['reason'][:200]}]" if r['now'] != 'allow' else ""))
    return "\n".join(lines)
