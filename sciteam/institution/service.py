"""InstitutionService: planners of institution Acts, resolution of pending Acts, recording of
derived facts and ledger entries, amendment enactment (docs/40-institution.md §6, §8, §9).
Every event it causes goes through CommService (`submit`, `resolve`, `record`)."""

import difflib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sciteam.comm.board import check_transition, target_state
from sciteam.comm.model import TERMINAL, Act, Message, deny
from sciteam.comm.policy import Check, SelCtx, SelectorError, compile_selector
from sciteam.comm.service import CommService, Plan, Reject
from sciteam.institution.amend import (
    CHANGES_HELP,
    admits,
    condition_keys,
    idle_validators,
    apply_changes,
    finished_runs,
    readjudicate,
    render_review,
)
from sciteam.institution.claims import ClaimSpec, parse_spec, render_counterexample
from sciteam.institution.defects import decision_record, defect_acts
from sciteam.institution.gate import act_task
from sciteam.institution.model import RuleSet, protected_changes, text_sha
from sciteam.institution.view import FOLD_EVENT_TYPES, INST_STREAM, Amendment, InstitutionView

MAX_SETTLE_ROUNDS = 100
MAX_TRIALS = 40          # validator runs one amendment may request on recorded commits
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".R", ".r", ".jl", ".ipynb", ".m"})


def must(cond: bool, reason: str, rule: str = "constitution") -> None:
    if not cond:
        raise Reject(deny(reason, rule))


def amendment_stream(aid: str) -> str:
    return f"amendment/{aid}"


def trial_ref(aid: str) -> str:
    """The act id trial validations of an amendment carry."""
    return f"amend:{aid}"


class InstitutionService:
    def __init__(self, comm: CommService, inst: InstitutionView):
        self.comm, self.inst = comm, inst
        self.version_listeners: list[Callable[[RuleSet], None]] = []
        # (path, rule set after the change) -> reason the change cannot take effect now
        self.amendment_checks: list[Callable[[str, RuleSet], str | None]] = []
        self._applied: str | None = None
        self._checked: dict[str, int] = {}          # pending act -> latest wait seq seen
        comm.extra_kinds.update({
            "propose_amendment": self._plan_propose_amendment, "vote": self._plan_vote,
            "withdraw_amendment": self._plan_withdraw_amendment,
            "decide_pending": self._plan_decide_pending,
            "certify_environment": self._plan_certify_environment,
            "revoke_environment": self._plan_revoke_environment,
            "propose_acceptance": self._plan_propose_acceptance,
            "record_claim": self._plan_record_claim, "attest": self._plan_attest,
            "overturn": self._plan_overturn, "overturn_claim": self._plan_overturn_claim,
            "goal_verdict": self._plan_goal_verdict})
        comm.gated_kinds.update({"certify_environment", "propose_acceptance", "record_claim",
                                 "attest"})
        comm.followers.append(self._follow)
        comm.catch_up()

    # ---- fold ---------------------------------------------------------------------------
    def _follow(self) -> None:
        for ev in self.comm.store.read_global(after_seq=self.inst.cursor,
                                              types=FOLD_EVENT_TYPES):
            self.inst.apply(ev)
        rs = self.inst.rules
        if rs is not None and rs.version != self._applied:
            self._applied = rs.version
            self.comm.set_policy(rs.comm)
            for listener in self.version_listeners:
                listener(rs)

    def start(self, rs: RuleSet) -> None:
        """Activate the configured rule set once per run; later versions come from amendments."""
        with self.comm.lock:
            self.comm.catch_up()
            if self.inst.rules is None:
                self._activate(rs, None, None)

    def _activate(self, rs: RuleSet, amendment: str | None, cause: int | None) -> None:
        self.comm.record(INST_STREAM, "rule_version_activated", {
            "version": rs.version, "files": rs.files, "texts": rs.texts,
            "amendment": amendment}, causation=cause)

    def selector_holds(self, src: str, member: str, actor: str, task=None) -> bool:
        return compile_selector(src, self.comm.policy.selectors)(
            member, SelCtx(view=self.comm.view, actor=actor, task=task))

    # ---- settle: consequences of recorded events ------------------------------------------
    def settle(self) -> int:
        """Write what the recorded events now imply; returns the number of rounds that wrote."""
        wrote = 0
        with self.comm.lock:
            for _ in range(MAX_SETTLE_ROUNDS):
                self.comm.catch_up()
                n = self._resolve_pending() + self._write_owed() + self._amendments()
                if not n:
                    break
                wrote += 1
        return wrote

    def _resolve_pending(self) -> int:
        n = 0
        for aid, p in list(self.inst.pending.items()):
            validations = self.inst.validations_for(aid)
            approval = self.inst.approvals.get(aid)
            missing, open_, failed, latest = False, False, False, p.decided_seq
            for w in p.waiting:
                kind, name = w.split(":", 1)
                if kind == "validate":
                    rec = validations.get(name)
                    status, seq = (rec.status, rec.done_seq) if rec else (None, 0)
                elif kind == "claim":
                    c = self.inst.claims.get(name)
                    status, seq = (c.status, c.done_seq) if c else (None, 0)
                else:
                    status, seq = (approval.status, approval.done_seq) if approval else (None, 0)
                missing |= status is None
                open_ |= status == "requested"
                failed |= status in ("failed", "rejected")
                latest = max(latest, seq)
            for rec in validations.values():
                failed |= rec.status == "failed"
                latest = max(latest, rec.done_seq)
            if approval is not None:
                open_ |= approval.status == "requested"
                failed |= approval.status == "rejected"
                latest = max(latest, approval.done_seq)
            task = act_task(p.act, self.comm.view)
            gone = task is not None and task.status in TERMINAL
            changed = latest > self._checked.get(aid, 0)
            if not (missing or failed or gone or (changed and not open_)):
                continue
            self._checked[aid] = latest
            before = self.inst.cursor
            self.comm.resolve(p.act, latest)
            n += self.inst.cursor > before
        return n

    def _write_owed(self) -> int:
        n = 0
        for key, owed in sorted(self.inst.owed.items(), key=lambda kv: kv[1].cause):
            if key in self.inst.owed:
                self.comm.record(INST_STREAM, owed.type, owed.payload, causation=owed.cause)
                n += 1
        return n

    def record_validation(self, validation_id: str, *, ok: bool, exit_code: int | None,
                          wall_s: float, output_blob: str | None, tail: str,
                          cached_from: str | None = None, infra: bool = False,
                          inputs: str | None = None) -> None:
        with self.comm.lock:
            self.comm.catch_up()
            v = self.inst.validations[validation_id]
            if v.status != "requested":
                return
            self.comm.record(INST_STREAM, "validation_finished", {
                "validation_id": validation_id, "ok": ok, "exit_code": exit_code,
                "wall_s": round(wall_s, 2), "output_blob": output_blob, "tail": tail,
                "cached_from": cached_from, "infra": infra, "inputs": inputs},
                causation=v.seq)

    def record_replay(self, claim_id: str, *, code: str, observed: Any, exit_code: int | None,
                      tail: str, sources: dict[str, str] | None = None) -> None:
        with self.comm.lock:
            self.comm.catch_up()
            c = self.inst.claims[claim_id]
            if c.status != "requested":
                return
            self.comm.record(INST_STREAM, "claim_replayed", {
                "claim_id": claim_id, "code": code, "observed": observed,
                "exit_code": exit_code, "tail": tail, "sources": sources or {}},
                causation=c.seq)

    # ---- amendments -----------------------------------------------------------------------
    def _amendments(self) -> int:
        rs = self.inst.rules
        spec = rs.constitution.amendment if rs and rs.constitution else None
        if spec is None:
            return 0
        n, now = 0, self.comm.clock.now()
        for a in list(self.inst.amendments.values()):
            stream = amendment_stream(a.id)
            if a.status == "trial":
                n += self._review_trial(a)
            elif a.status == "open":
                if (now - a.proposed_at).total_seconds() > spec.voting_window_s:
                    self.comm.record(stream, "amendment_closed",
                                     {"amendment_id": a.id, "reason": "expired"},
                                     causation=a.last_seq)
                    n += 1
                elif self.tally(a)["passes"]:
                    if spec.ratify(a.path) or a.loosens:
                        ev = self.comm.record(stream, "amendment_passed",
                                              {"amendment_id": a.id, **self.tally(a)},
                                              causation=a.last_seq)
                        self.comm.record(INST_STREAM, "approval_requested", {
                            "key": a.id, "approval": "ratify", "approver": "kind:human",
                            "actor": a.proposer,
                            "summary": f"ratify amendment {a.id} to {a.path}: {a.rationale}"},
                            causation=ev.seq)
                    else:
                        self._enact(a, a.last_seq)
                    n += 1
            elif a.status == "ratifying":
                st = self.inst.approvals.get(a.id)
                if st is not None and st.status == "approved":
                    self._enact(a, st.done_seq)
                    n += 1
                elif st is not None and st.status == "rejected":
                    self.comm.record(stream, "amendment_closed", {
                        "amendment_id": a.id, "reason": "not_ratified", "note": st.note},
                        causation=st.done_seq)
                    n += 1
        return n

    def voters(self, a: Amendment) -> list[str]:
        spec = self.inst.rules.constitution.amendment
        return sorted(m for m in self.comm.view.members
                      if self.selector_holds(spec.voters, m, a.proposer))

    def tally(self, a: Amendment) -> dict[str, Any]:
        rule = self.inst.rules.constitution.amendment.pass_
        voters = self.voters(a)
        yes = sum(1 for v in voters if a.votes.get(v) == "yes")
        no = sum(1 for v in voters if a.votes.get(v) == "no")
        need = len(voters) if rule.yes_at_least == "all" else rule.yes_at_least
        return {"yes": yes, "no": no, "voters": len(voters), "yes_needed": need,
                "passes": yes >= need and no <= rule.no_at_most}

    def _enact(self, a: Amendment, cause: int) -> None:
        # 同一轮可生效多个修正案：每个都作用在现行版本上，而不是本轮开始时的快照
        self.comm.catch_up()
        rs = self.inst.rules
        stream = amendment_stream(a.id)
        if rs.files.get(a.path) != a.base_sha:
            self.comm.record(stream, "amendment_closed", {
                "amendment_id": a.id, "reason": "conflict",
                "note": f"{a.path} changed since the proposal; propose again"}, causation=cause)
            return
        try:
            new = rs.with_file(a.path, a.content)
        except (ValidationError, ValueError, yaml.YAMLError) as e:
            self.comm.record(stream, "amendment_closed", {
                "amendment_id": a.id, "reason": "invalid", "note": str(e)[:1000]},
                causation=cause)
            return
        blocked = self._check_change(a.path, new)
        if blocked:
            self.comm.record(stream, "amendment_closed", {
                "amendment_id": a.id, "reason": "invalid", "note": blocked}, causation=cause)
            return
        ev = self.comm.record(stream, "amendment_enacted", {
            "amendment_id": a.id, "path": a.path, "new_sha": new.files[a.path],
            "rule_version": new.version, "proposer": a.proposer}, causation=cause)
        self._activate(new, a.id, ev.seq)

    # ---- planners of institution Acts -----------------------------------------------------
    def _plan_propose_amendment(self, act: Act) -> Plan:
        rs, p = self.inst.rules, act.params
        spec = rs.constitution.amendment if rs and rs.constitution else None
        must(spec is not None, "the constitution does not allow amendments")
        must(self.selector_holds(spec.proposers, act.actor, act.actor),
             f"only {spec.proposers} may propose amendments")
        changes = p.get("changes")
        path = str(p.get("path") or (rs.rules_path if changes else "")).strip()
        must(path in spec.amendable, f"'{path}' is not amendable; amendable: {spec.amendable}")
        must(path in rs.texts, f"'{path}' is not part of the rule set in force")
        if changes:
            must(path == rs.rules_path, f"structured changes apply to the rules file "
                 f"{rs.rules_path}; for {path} give content", "structure")
            try:
                content = apply_changes(rs.texts[path], changes)
            except (ValueError, yaml.YAMLError) as e:
                raise Reject(deny(f"{str(e)[:1500]}\n{CHANGES_HELP}", "schema")) from None
        else:
            content = str(p.get("content", ""))
        must(bool(content.strip()), "give changes=[...] or the complete new content of the file")
        must(text_sha(content) != rs.files[path], f"the content is identical to {path} in force")
        try:
            new = rs.with_file(path, content)
        except (ValidationError, ValueError, yaml.YAMLError, SelectorError) as e:
            hint = (f"\na condition has exactly one of the keys {condition_keys()} (see rules)"
                    if "require" in str(e) and "extra_forbidden" in str(e) else "")
            raise Reject(deny(f"invalid {path}: {str(e)[:1500]}{hint}", "schema")) from None
        blocked = self._check_change(path, new)
        must(blocked is None, f"{path}: {blocked}", "schema")
        kinds, before = self.comm.act_kinds(), rs.rules.named()
        stray = [f"{r.name} (act: {r.act})" for r in [*new.rules.requirements, *new.rules.approvals]
                 if r.act not in kinds and (before.get(r.name) or {}).get("act") != r.act]
        must(not stray, f"{path}: rules name no Act kind there is: {', '.join(stray)}; act is "
             f"one of {', '.join(sorted(kinds))}", "schema")
        changed = protected_changes(spec.protected_rules, rs.rules.named(), new.rules.named())
        must(not changed, f"protected rules must stay present and unchanged: {changed}",
             "protected_rules")
        cited = [str(d).strip().upper() for d in (p.get("defects") or [])]
        unknown = [d for d in cited if d not in self.inst.defects]
        must(not unknown, f"unknown defect records {unknown}; see defects", "structure")
        review = None
        aid = f"A{len(self.inst.amendments) + 1}"
        trials: list[dict[str, Any]] = []
        if spec.admission is not None and path == rs.rules_path:
            must(bool(cited), "cite the defect records this amendment fixes: defects=['D1', ...] "
                 "(see defects)", "admission")
            review = self.readjudicate(new, cited)
            trials = review["trials"][:MAX_TRIALS]
            if not trials:
                why = admits(review, spec.admission)
                idle = idle_validators(rs, new) if why and not review["fixed"] else []
                if idle:
                    why += (f"\nno rule runs {', '.join(idle)}: add, in the same changes, a "
                            f"rule with the condition {{validate: [{idle[0]}]}} on the Act it "
                            f"checks (e.g. act: task_transition, transition: submit)")
                must(why is None, f"{why}\n{render_review(review)}", "admission")
        voter = self.selector_holds(spec.voters, act.actor, act.actor)
        diff = "".join(difflib.unified_diff(rs.texts[path].splitlines(True),
                                            content.splitlines(True), f"{path} (in force)",
                                            f"{path} ({aid})"))
        plan = Plan()
        plan.add(amendment_stream(aid), "amendment_proposed", {
            "amendment_id": aid, "path": path, "content": content,
            "base_sha": rs.files[path], "new_sha": text_sha(content),
            "proposer": act.actor, "rationale": str(p.get("rationale", "")),
            "votes": {act.actor: "yes"} if voter else {}, "defects": cited,
            "changes": changes or None, "review": None if trials else review,
            "diff": diff[:6000], "trial": bool(trials)})
        n = len(self.inst.validations)
        for i, t in enumerate(trials, n + 1):
            plan.add(INST_STREAM, "validation_requested", {
                "validation_id": f"V{i}", "validator": t["validator"],
                "act_id": trial_ref(aid), "commit": t["commit"], "command": t["command"],
                "timeout_s": t["timeout_s"]})
        text = (f"{len(trials)} validator run(s) the new rules need on recorded commits are "
                f"running; admission is decided when they finish: "
                + ", ".join(f"{t['validator']}@{t['commit'][:10]}" for t in trials)) \
            if trials else (render_review(review) if review else "")
        plan.data = {"amendment": aid, "diff": diff[:6000], "review": text}
        return plan

    def _review_trial(self, a: Amendment) -> bool:
        """Once its trial validations have finished, admit the amendment to voting or close it."""
        runs = [v for v in self.inst.validations.values() if v.act_id == trial_ref(a.id)]
        if any(v.status == "requested" for v in runs):
            return False
        stream, cause = amendment_stream(a.id), max([a.last_seq, *(v.done_seq for v in runs)])
        rs = self.inst.rules
        if rs.files.get(a.path) != a.base_sha:
            self.comm.record(stream, "amendment_closed", {
                "amendment_id": a.id, "reason": "conflict",
                "note": f"{a.path} changed since the proposal; propose again"}, causation=cause)
            return True
        review = self.readjudicate(rs.with_file(a.path, a.content), a.defects)
        why = admits(review, rs.constitution.amendment.admission)
        if why is None and review["trials"]:
            why = (f"validators never ran on: " + ", ".join(
                f"{t['validator']}@{t['commit'][:10]}" for t in review["trials"]))
        if why is not None:
            self.comm.record(stream, "amendment_closed", {
                "amendment_id": a.id, "reason": "admission",
                "note": f"{why}\n{render_review(review)}"[:3000], "review": review},
                causation=cause)
        else:
            self.comm.record(stream, "amendment_reviewed", {
                "amendment_id": a.id, "review": review}, causation=cause)
        return True

    def readjudicate(self, new: RuleSet, defects: list[str]) -> dict[str, Any]:
        """Re-decide the project's recorded Acts under `new` (pure: no model, no sandbox)."""
        acts: set[str] = set()
        for d in defects:
            acts |= defect_acts(self.inst, self.inst.defects[d])
        comm = self.comm
        return readjudicate(
            comm.store.read_global(after_seq=0, types=FOLD_EVENT_TYPES), new,
            constitution_path=self.inst.constitution_path, comm_path=self.inst.comm_path,
            resolve_commit=comm.extra_gate.resolve_commit,
            gated=lambda k: k not in comm.extra_kinds or k in comm.gated_kinds,
            defect_acts=acts, runs=finished_runs(self.inst.validations.values()))

    def _check_change(self, path: str, new: RuleSet) -> str | None:
        return next((r for check in self.amendment_checks if (r := check(path, new))), None)

    # ---- environment certificates (50 §5) -------------------------------------------------
    def _plan_certify_environment(self, act: Act) -> Plan:
        rs = self.inst.rules
        env = str(act.params.get("env", "")).strip()
        must(rs is not None and env in rs.rules.environments,
             f"unknown environment '{env}'; defined: "
             f"{sorted(rs.rules.environments) if rs else []}", "structure")
        self.comm.policy_check(Check(act="certify_environment", actor=act.actor))
        done = self.inst.validations_for(act.act_id)
        commit = next((v.commit for v in done.values()), None) or str(act.params.get("commit"))
        plan = Plan()
        plan.add(INST_STREAM, "environment_certified", {
            "env": env, "commit": commit, "by": act.actor,
            "fingerprint": rs.rules.env_fingerprint(env), "rule_version": rs.version,
            "validations": {n: v.id for n, v in done.items()},
            "note": str(act.params.get("note", ""))})
        plan.data = {"env": env}
        return plan

    def _plan_revoke_environment(self, act: Act) -> Plan:
        env = str(act.params.get("env", "")).strip()
        must(env in self.inst.certificates, f"no '{env}' certificate has been issued",
             "structure")
        self.comm.policy_check(Check(act="revoke_environment", actor=act.actor))
        reason = str(act.params.get("reason", "")).strip()
        must(bool(reason), "revoking needs a reason", "structure")
        plan = Plan()
        plan.add(INST_STREAM, "environment_revoked", {"env": env, "by": act.actor,
                                                      "reason": reason})
        plan.data = {"env": env}
        return plan

    # ---- acceptance specs and claims (09 §4) -----------------------------------------------
    def _spec_task(self, act: Act):
        task = act_task(act, self.comm.view)
        must(task is not None, f"unknown task '{act.params.get('task')}'", "structure")
        must(task.status not in TERMINAL, f"{task.id} is {task.status}", "structure")
        return task

    def _plan_propose_acceptance(self, act: Act) -> Plan:
        task = self._spec_task(act)
        self.comm.policy_check(Check(act="propose_acceptance", actor=act.actor, task=task))
        try:
            spec = parse_spec(act.params.get("spec"))
        except (ValidationError, ValueError) as e:
            raise Reject(deny(f"invalid acceptance spec: {str(e)[:1500]}", "schema")) from None
        data = spec.model_dump(mode="json")
        sha = text_sha(json.dumps(data, sort_keys=True))[:12]
        cur = self.inst.specs.get(task.id)
        must(cur is None or cur["spec_sha"] != sha,
             f"{task.id} already has this spec ({sha})", "structure")
        plan = Plan()
        plan.add(INST_STREAM, "acceptance_set", {
            "task_id": task.id, "spec": data, "spec_sha": sha, "by": act.actor,
            "replaces": cur["spec_sha"] if cur else None})
        plan.data = {"task": task.id, "spec_sha": sha}
        return plan

    def _plan_record_claim(self, act: Act) -> Plan:
        task = self._spec_task(act)
        self.comm.policy_check(Check(act="record_claim", actor=act.actor, task=task))
        spec, cur = self.inst.spec(task.id), self.inst.specs.get(task.id)
        must(spec is not None, f"{task.id} has no acceptance spec; @{task.created_by} or "
             f"@{task.assignee} proposes one with propose_acceptance", "structure")
        name = str(act.params.get("claim", "")).strip()
        adhoc = None
        if act.actor == task.assignee:
            must(spec.claim(name) is not None, f"{task.id}'s spec names no claim '{name}'; it "
                 f"names {', '.join(c.name for c in spec.claims)}", "structure")
        else:
            adhoc = self._adhoc_claim(spec, name, act.params)
        try:
            value = float(act.params.get("value"))
        except (TypeError, ValueError):
            raise Reject(deny("value must be a number", "structure")) from None
        must(value == value and abs(value) != float("inf"), "value must be finite", "structure")
        ref = str(act.params.get("commit") or "").strip()
        must(bool(ref), "give commit=<git revision in /workspace whose reproduction yields "
             "the value>", "structure")
        sha = self.comm.extra_gate.resolve_commit(ref)
        must(sha is not None, f"'{ref}' is not a commit in the /workspace repository",
             "structure")
        reproduce = adhoc["reproduce"] if adhoc else spec.reproduce
        missing = self.comm.extra_gate.uncommitted_named(sha, reproduce)
        must(not missing, f"commit {sha[:10]} lacks {', '.join(missing)}, which the reproduce "
             f"command `{reproduce}` runs or reads: its replay runs on a clean export of that "
             "commit, so git add and commit them, then record the claim on the new commit",
             "structure")
        cid = f"C{len(self.inst.claims) + 1}"
        plan = Plan()
        plan.add(INST_STREAM, "claim_recorded", {
            "claim_id": cid, "task_id": task.id, "name": name, "value": value, "commit": sha,
            "actor": act.actor, "spec_sha": cur["spec_sha"], "adhoc": adhoc})
        plan.data = {"claim": cid, "task": task.id}
        return plan

    @staticmethod
    def _adhoc_claim(spec, name: str, p: dict) -> dict[str, Any]:
        """A reviewer's own claim: their reproduction command and output location, defaulting
        to the spec's for a quantity the spec names."""
        base = spec.claim(name)
        reproduce = str(p.get("reproduce") or spec.reproduce).strip()
        file = str(p.get("file") or (base.file if base else "")).strip()
        pointer = p.get("pointer") if p.get("pointer") is not None else \
            (base.pointer if base else None)
        must(bool(file) and pointer is not None,
             f"'{name}' is not in the spec; give file=<JSON file your reproduce command writes> "
             f"and pointer=<JSON Pointer to the number>", "structure")
        try:
            ClaimSpec(name=name, file=file, pointer=str(pointer))
        except (ValidationError, ValueError) as e:
            raise Reject(deny(f"invalid claim: {str(e)[:600]}", "schema")) from None
        return {"reproduce": reproduce, "file": file, "pointer": str(pointer)}

    def _plan_attest(self, act: Act) -> Plan:
        task = self._spec_task(act)
        self.comm.policy_check(Check(act="attest", actor=act.actor, task=task))
        v = self.comm.view
        must(task.status == "in_review", f"{task.id} is {task.status}; its claims are attested "
             f"while it is in review", "structure")
        commit = self.inst.review_commit(task.id)
        must(commit is not None, f"@{task.assignee} recorded no claims on {task.id} under its "
             f"spec in force, so {task.id} has nothing to attest: review it by its acceptance "
             f"command and approve or reject it with task_transition", "structure")
        on = self.inst.claims_on(task.id, commit)
        ref = str(act.params.get("claim", "")).strip()
        c = on.get(ref) or next((x for x in on.values() if x.id == ref.upper()), None)
        must(c is not None, f"no claim '{ref}' on {task.id}'s commit {commit[:10]}; claims: "
             f"{', '.join(f'{n} ({x.id})' for n, x in sorted(on.items()))}", "structure")
        must(c.status != "failed", f"{c.id} {c.name} did not replay itself ({c.code}), so it "
             f"needs no counterexample: send {task.id} back with task_transition("
             f"task='{task.short}', transition='reject', note=<what failed>)", "structure")
        must(c.status != "requested", f"{c.id} {c.name} is still replaying; attest once its "
             f"replay is done", "structure")
        verdict = str(act.params.get("verdict", "")).strip()
        must(verdict in ("pass", "reject", "abstain"), "verdict must be pass, reject or abstain",
             "structure")
        basis = str(act.params.get("basis", "")).strip()
        must(bool(basis), "give basis=<id of a claim you recorded, or a file of your own "
             "reproduction>", "structure")
        if verdict == "pass" and act.actor != task.assignee:   # the author meets independent_of
            b = self.inst.claims.get(basis.upper())
            must(b is not None and b.actor == act.actor and b.task_id == task.id,
                 f"a pass rests on your own reproduction: basis={basis} is not a claim you "
                 f"recorded on {task.id}; record_claim your reproduction and attest once it "
                 f"has replayed", "structure")
            spec = self.inst.spec(task.id)
            command = b.adhoc["reproduce"] if b.adhoc else (spec.reproduce if spec else "")
            theirs = [f for f in self.comm.extra_gate.committed_named(b.commit, command)
                      if Path(f).suffix in SCRIPT_SUFFIXES]
            must(not theirs, f"basis {b.id} runs {', '.join(theirs)} from the author's commit, "
                 f"which repeats the author's computation: write your own script in your "
                 f"private directory (it may read the data in /workspace), record it and "
                 f"attest with that claim", "structure")
            must(b.status == "passed", f"basis {b.id} did not replay "
                 f"({b.code or b.status}); a pass rests on a replayed claim", "structure")
            must(b.commit == commit, f"basis {b.id} was recorded on commit {b.commit[:10]}; "
                 f"{task.id} is in review on {commit[:10]}, so record your reproduction on "
                 f"commit={commit[:10]} and attest with it", "structure")
            base = getattr(self.comm.extra_gate, "private_base", None)
            if base:
                mine = f"{base.rstrip('/')}/{act.actor}/"
                must(mine in command, f"basis {b.id} runs `{command}`, which runs nothing "
                     f"from your private directory {mine}: an independent reproduction is a "
                     f"script you wrote there; record it and attest with that claim",
                     "structure")
            if b.name == c.name and spec is not None:
                agree = math.isclose(b.value, c.value, rel_tol=spec.rel_tol,
                                     abs_tol=spec.abs_tol)
                must(agree, f"your reproduction {b.id} gives {c.name} = {b.value:g}, and the "
                     f"author claims {c.value:g}: they disagree, so attest verdict=reject "
                     f"with counterexample={b.id}", "structure")
        adjudicator = self.selector_holds("role:adjudicator", act.actor, act.actor, task)
        must(not (adjudicator and verdict == "abstain"),
             "as adjudicator, decide: verdict=pass, or reject with a counterexample", "structure")
        cx = None
        if verdict == "reject":
            cid = str(act.params.get("counterexample") or "").strip().upper()
            own = self.inst.claims.get(basis.upper())
            must(bool(cid), "reject needs counterexample=<id of a claim you recorded whose replay "
                 "shows the defect>" + (f"; give counterexample={own.id}" if own and
                 own.actor == act.actor else "; without one, use verdict=abstain"), "structure")
            cx = self.inst.claims.get(cid)
            must(cx is not None and cx.actor == act.actor and cx.task_id == task.id and
                 cx.adhoc is not None,
                 f"{cid} is not a claim you recorded on {task.id}", "structure")
            must(cx.status != "requested", f"counterexample {cid} is still replaying; attest "
                 f"again once you are notified of its replay", "structure")
            must(cx.status == "passed", f"counterexample {cid} did not replay ({cx.code}); a "
                 f"counterexample must replay", "structure")
            must(cx.commit == commit, f"counterexample {cid} was recorded on commit "
                 f"{cx.commit[:10]}; {task.id} is in review on {commit[:10]}, so record it on "
                 f"commit={commit[:10]}", "structure")
        rounds, limit = self.inst.returns(task.id), self.inst.rules.rules.rework_rounds
        routed = ""
        if verdict == "reject" and (adjudicator or rounds < limit):
            routed = "author"
        elif verdict != "pass":
            routed = "adjudication"
        aid = f"AT{len(self.inst.attestations) + 1}"
        plan = Plan()
        plan.add(INST_STREAM, "attestation_recorded", {
            "attestation_id": aid, "task_id": task.id, "claim_id": c.id, "name": c.name,
            "commit": commit, "verdict": verdict, "basis": basis,
            "counterexample": cx.id if cx else None, "by": act.actor, "routed": routed})
        if routed == "author":
            err = check_transition(task, "reject", v.tasks)
            must(err is None, err or "", "structure")
            note = render_counterexample(c, cx)
            plan.add(f"task/{task.id}", "task_status_changed", {
                "task_id": task.id, "transition": "reject", "frm": task.status,
                "to": target_state(task, "reject"), "note": note})
            n = len(v.msg_index) + 1
            if act.actor not in v.convs[task.conv].members:
                plan.add(f"conv/{task.conv}", "conv_member_added",
                         {"conv": task.conv, "member": act.actor})
            plan.add(f"conv/{task.conv}", "message_posted", {"message": Message(
                msg_id=f"m{n}", conv=task.conv, author=act.actor, text=f"[reject] {note}",
                thread=f"m{n}").model_dump(mode="json", exclude={"seq", "ts"})})
        elif routed == "adjudication":
            why = "abstain: disagreement without a counterexample" if verdict == "abstain" \
                else f"rework rounds used ({rounds} of {limit})"
            plan.add(INST_STREAM, "adjudication_requested", {
                "task_id": task.id, "claim_id": c.id, "name": c.name, "attestation_id": aid,
                "by": act.actor, "reason": why})
        plan.data = {"attestation": aid, "task": task.id, "routed": routed}
        return plan

    # ---- defect records (docs/09 §5.1) --------------------------------------------------
    def _plan_overturn(self, act: Act) -> Plan:
        """The owner overturns an accepted result: a defect record, and the task reopens."""
        must(self.selector_holds("kind:human", act.actor, act.actor),
             "only a human member overturns an accepted result", "structure")
        return self._overturn(act, "human_overruled", None)

    def _plan_goal_verdict(self, act: Act) -> Plan:
        """A human decides on a delivered result: accept records the verdict the rules turn
        into a fact; reject records a defect and reopens the task with the reason."""
        must(self.selector_holds("kind:human", act.actor, act.actor),
             "only a human member gives the verdict on a delivered result", "structure")
        verdict = str(act.params.get("verdict", "")).strip()
        must(verdict in ("accept", "reject"), "verdict must be accept or reject", "structure")
        if verdict == "reject":
            return self._overturn(act, "acceptance_rejected", None)
        task = act_task(act, self.comm.view)
        must(task is not None, f"unknown task '{act.params.get('task')}'", "structure")
        must(task.status == "done", f"{task.id} is {task.status}; a verdict is given on a "
             f"delivered (done) result", "structure")
        plan = Plan()
        plan.add(INST_STREAM, "goal_verdict", {
            "task_id": task.id, "verdict": "accept", "to": "accepted", "by": act.actor,
            "note": str(act.params.get("note") or act.params.get("reason") or "")[:4000]})
        plan.data = {"task": task.id, "verdict": "accept"}
        return plan

    def _plan_overturn_claim(self, act: Act) -> Plan:
        """The lead records that a later accepted claim contradicts an accepted one."""
        must(self.selector_holds("role:lead | kind:human", act.actor, act.actor),
             "only the lead or a human records an overturned claim", "structure")
        ref = str(act.params.get("by_claim", "")).strip().upper()
        cx = self.inst.claims.get(ref)
        must(cx is not None and cx.status == "passed",
             f"by_claim must be a claim that replayed (got '{ref}')", "structure")
        return self._overturn(act, "claim_overturned", cx)

    def _overturn(self, act: Act, source: str, cx) -> Plan:
        v = self.comm.view
        task = act_task(act, v)
        must(task is not None, f"unknown task '{act.params.get('task')}'", "structure")
        must(task.status == "done" or self.inst.has_fact("accepted", task.id),
             f"{task.id} is {task.status}; only an accepted result can be overturned",
             "structure")
        if cx is not None:
            must(cx.task_id != task.id, "by_claim must come from another task", "structure")
        reason = str(act.params.get("reason", "")).strip()
        must(bool(reason), "give reason=<what is wrong with the accepted result>", "structure")
        claim = None
        ref = str(act.params.get("claim", "")).strip()
        if ref:
            commit = self.inst.review_commit(task.id)
            on = self.inst.claims_on(task.id, commit) if commit else {}
            claim = on.get(ref) or next((c for c in on.values() if c.id == ref.upper()), None)
            must(claim is not None, f"no accepted claim '{ref}' on {task.id}; claims: "
                 f"{', '.join(f'{n} ({c.id})' for n, c in sorted(on.items())) or 'none'}",
                 "structure")
        did = f"D{len(self.inst.defects) + 1}"
        plan = Plan()
        plan.add(INST_STREAM, "defect_recorded", {
            "defect_id": did, "source": source, "task_id": task.id,
            "claim_id": claim.id if claim else None, "by": act.actor, "reason": reason,
            "counterexample": cx.id if cx else None,
            "decision": decision_record(self.inst, v, task.id, claim.id if claim else None)})
        if task.status == "done":
            err = check_transition(task, "reopen", v.tasks)
            must(err is None, err or "", "structure")
            note = f"overturned by @{act.actor} ({did}): {reason}"
            plan.add(f"task/{task.id}", "task_status_changed", {
                "task_id": task.id, "transition": "reopen", "frm": task.status,
                "to": target_state(task, "reopen"), "note": note})
            n = len(v.msg_index) + 1
            if act.actor not in v.convs[task.conv].members:
                plan.add(f"conv/{task.conv}", "conv_member_added",
                         {"conv": task.conv, "member": act.actor})
            plan.add(f"conv/{task.conv}", "message_posted", {"message": Message(
                msg_id=f"m{n}", conv=task.conv, author=act.actor, text=f"[reopen] {note}",
                thread=f"m{n}").model_dump(mode="json", exclude={"seq", "ts"})})
        plan.data = {"defect": did, "task": task.id}
        return plan

    def record_replication(self, claim_id: str, *, code: str, observed: Any,
                           tail: str) -> str | None:
        """A periodic replication of an accepted claim; a mismatch is a defect record."""
        with self.comm.lock:
            self.comm.catch_up()
            c = self.inst.claims[claim_id]
            ev = self.comm.record(INST_STREAM, "claim_replicated", {
                "claim_id": c.id, "task_id": c.task_id, "name": c.name, "commit": c.commit,
                "code": code, "observed": observed, "tail": tail[-1500:]}, causation=c.seq)
            if code in ("ok", "infra"):
                return None
            did = f"D{len(self.inst.defects) + 1}"
            self.comm.record(INST_STREAM, "defect_recorded", {
                "defect_id": did, "source": "replication_failed", "task_id": c.task_id,
                "claim_id": c.id, "by": "system",
                "reason": f"replicating {c.name} = {c.value:g} on commit {c.commit[:10]} gave "
                          f"{observed!r} ({code})", "counterexample": None,
                "decision": decision_record(self.inst, self.comm.view, c.task_id, c.id)},
                causation=ev.seq)
            return did

    def accepted_claims(self, due_after_s: float | None = None, now: float = 0.0) -> list:
        """Claims of accepted tasks, as last attested: what a replication run replays. With
        `due_after_s`, only those last replicated (or accepted) at least that long before
        `now`, by the wall clock of the event log."""
        out = []
        for (fact, task_id) in list(self.inst.facts):
            if fact != "accepted":
                continue
            commit = self.inst.review_commit(task_id)
            if not commit:
                continue
            for c in self.inst.claims_on(task_id, commit).values():
                if c.status != "passed":
                    continue
                if due_after_s is not None:
                    rep = self.inst.replications.get(c.id)
                    last = rep["at"] if rep else self.inst.fact_ts.get((fact, task_id), now)
                    if now - last < due_after_s:
                        continue
                out.append(c)
        return out

    def _open_amendment(self, ref: Any) -> Amendment:
        a = self.inst.amendments.get(str(ref or "").strip().upper())
        must(a is not None, f"unknown amendment '{ref}'", "structure")
        return a

    def _plan_vote(self, act: Act) -> Plan:
        a = self._open_amendment(act.params.get("amendment"))
        spec = self.inst.rules.constitution.amendment
        must(a.status == "open", f"{a.id} is {a.status}; voting is closed")
        must(self.selector_holds(spec.voters, act.actor, a.proposer),
             f"only {spec.voters} may vote")
        choice = str(act.params.get("choice", ""))
        must(choice in ("yes", "no"), "choice must be yes or no", "structure")
        plan = Plan()
        plan.add(amendment_stream(a.id), "amendment_voted", {
            "amendment_id": a.id, "voter": act.actor, "choice": choice,
            "note": str(act.params.get("note", ""))})
        plan.data = {"amendment": a.id}
        return plan

    def _plan_withdraw_amendment(self, act: Act) -> Plan:
        a = self._open_amendment(act.params.get("amendment"))
        must(a.proposer == act.actor, f"only the proposer (@{a.proposer}) may withdraw {a.id}")
        must(a.status in ("trial", "open", "ratifying"), f"{a.id} is already {a.status}")
        plan = Plan()
        plan.add(amendment_stream(a.id), "amendment_closed",
                 {"amendment_id": a.id, "reason": "withdrawn"})
        plan.data = {"amendment": a.id}
        return plan

    def _plan_decide_pending(self, act: Act) -> Plan:
        key = str(act.params.get("id", "")).strip()
        st = self.inst.approvals.get(key) or self.inst.approvals.get(key.upper())
        must(st is not None, f"nothing awaits approval under '{key}'", "structure")
        must(st.status == "requested", f"'{key}' was already decided ({st.status})")
        subject = self.inst.acts.get(st.key)
        task = act_task(subject, self.comm.view) if subject else None
        must(self.selector_holds(st.approver, act.actor, act.actor, task),
             f"only {st.approver} may decide '{st.key}'", st.approval)
        decision = str(act.params.get("decision", ""))
        must(decision in ("approve", "reject"), "decision must be approve or reject", "structure")
        note = str(act.params.get("note", "")).strip()
        must(decision == "approve" or bool(note), "reject needs a note explaining why",
             "structure")
        plan = Plan()
        plan.add(INST_STREAM, "approval_decided", {
            "key": st.key, "decision": decision, "by": act.actor, "note": note})
        plan.data = {"id": st.key, "decision": decision}
        return plan

    # ---- direct store writers for session bootstrap / platform gateway (I1 single writer) ---
    @staticmethod
    def append_rule_version_activated(store: Any, version: str, files: dict[str, str]) -> Any:
        return store.append(INST_STREAM, "rule_version_activated", {"version": version, "files": files})

    @staticmethod
    def append_approval_requested(store: Any, payload: dict[str, Any], *, actor: str = "agent") -> Any:
        return store.append(INST_STREAM, "approval_requested", payload, actor=actor)

    @staticmethod
    def append_validation_finished(store: Any, validator: str, passed: bool) -> Any:
        return store.append("validation", "validation_finished", {"validator": validator, "passed": passed})

    @staticmethod
    def append_external_act_and_decision(
        store: Any,
        *,
        actor: str,
        kind: str,
        params: dict[str, Any],
        correction: Any = None,
        telemetry: dict[str, Any] | None = None,
        decision_payload: dict[str, Any] | None = None,
    ) -> Any:
        ev_act = store.append(
            INST_STREAM,
            "act_submitted",
            {
                "actor": actor,
                "kind": kind,
                "params": params,
                "correction": correction,
                "telemetry": telemetry or {},
            },
            actor=actor,
        )
        if decision_payload is not None:
            store.append(
                INST_STREAM,
                "approval_decided",
                decision_payload,
                actor=actor,
                causation_id=ev_act.seq,
            )
        return ev_act

