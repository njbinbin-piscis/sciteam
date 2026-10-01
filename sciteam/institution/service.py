"""InstitutionService: planners of institution Acts, resolution of pending Acts, recording of
derived facts and ledger entries, amendment enactment (docs/40-institution.md §6, §8, §9).
Every event it causes goes through CommService (`submit`, `resolve`, `record`)."""

import difflib
from collections.abc import Callable
from typing import Any

import yaml
from pydantic import ValidationError

from sciteam.comm.model import TERMINAL, Act, deny
from sciteam.comm.policy import Check, SelCtx, SelectorError, compile_selector
from sciteam.comm.service import CommService, Plan, Reject
from sciteam.institution.gate import act_task
from sciteam.institution.model import RuleSet, text_sha
from sciteam.institution.view import FOLD_EVENT_TYPES, INST_STREAM, Amendment, InstitutionView

MAX_SETTLE_ROUNDS = 100


def must(cond: bool, reason: str, rule: str = "constitution") -> None:
    if not cond:
        raise Reject(deny(reason, rule))


def amendment_stream(aid: str) -> str:
    return f"amendment/{aid}"


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
            "revoke_environment": self._plan_revoke_environment})
        comm.gated_kinds.add("certify_environment")
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
                          cached_from: str | None = None, infra: bool = False) -> None:
        with self.comm.lock:
            self.comm.catch_up()
            v = self.inst.validations[validation_id]
            if v.status != "requested":
                return
            self.comm.record(INST_STREAM, "validation_finished", {
                "validation_id": validation_id, "ok": ok, "exit_code": exit_code,
                "wall_s": round(wall_s, 2), "output_blob": output_blob, "tail": tail,
                "cached_from": cached_from, "infra": infra}, causation=v.seq)

    # ---- amendments -----------------------------------------------------------------------
    def _amendments(self) -> int:
        rs = self.inst.rules
        spec = rs.constitution.amendment if rs and rs.constitution else None
        if spec is None:
            return 0
        n, now = 0, self.comm.clock.now()
        for a in list(self.inst.amendments.values()):
            stream = amendment_stream(a.id)
            if a.status == "open":
                if (now - a.proposed_at).total_seconds() > spec.voting_window_s:
                    self.comm.record(stream, "amendment_closed",
                                     {"amendment_id": a.id, "reason": "expired"},
                                     causation=a.last_seq)
                    n += 1
                elif self.tally(a)["passes"]:
                    if spec.ratify(a.path):
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
        path, content = str(p.get("path", "")).strip(), str(p.get("content", ""))
        must(path in spec.amendable, f"'{path}' is not amendable; amendable: {spec.amendable}")
        must(path in rs.texts, f"'{path}' is not part of the rule set in force")
        must(bool(content.strip()), "give the complete new content of the file")
        must(text_sha(content) != rs.files[path], f"the content is identical to {path} in force")
        try:
            new = rs.with_file(path, content)
        except (ValidationError, ValueError, yaml.YAMLError, SelectorError) as e:
            raise Reject(deny(f"invalid {path}: {str(e)[:1500]}", "schema")) from None
        blocked = self._check_change(path, new)
        must(blocked is None, f"{path}: {blocked}", "schema")
        old_rules, new_rules = rs.rules.named(), new.rules.named()
        changed = [r for r in spec.protected_rules
                   if r in old_rules and new_rules.get(r) != old_rules[r]]
        must(not changed, f"protected rules must stay present and unchanged: {changed}",
             "protected_rules")
        aid = f"A{len(self.inst.amendments) + 1}"
        voter = self.selector_holds(spec.voters, act.actor, act.actor)
        diff = "".join(difflib.unified_diff(rs.texts[path].splitlines(True),
                                            content.splitlines(True), f"{path} (in force)",
                                            f"{path} ({aid})"))
        plan = Plan()
        plan.add(amendment_stream(aid), "amendment_proposed", {
            "amendment_id": aid, "path": path, "content": content,
            "base_sha": rs.files[path], "new_sha": text_sha(content),
            "proposer": act.actor, "rationale": str(p.get("rationale", "")),
            "votes": {act.actor: "yes"} if voter else {}})
        plan.data = {"amendment": aid, "diff": diff[:6000]}
        return plan

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
        must(a.status in ("open", "ratifying"), f"{a.id} is already {a.status}")
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
