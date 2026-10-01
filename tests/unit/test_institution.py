from pathlib import Path

import pytest
from pydantic import ValidationError

from sciteam.comm import Act, CommService, Member
from sciteam.institution.gate import InstitutionGate
from sciteam.institution.model import RuleSet, RulesFile
from sciteam.institution.service import InstitutionService
from sciteam.institution.view import InstitutionView
from sciteam.kernel import EventStore, FakeClock
from sciteam.kernel.ids import new_id

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
CONST_PATH, RULES_PATH, COMM_PATH = ("institution/t.yaml", "institution/t-rules.yaml",
                                     "comm/flat.yaml")
CONST = f"""
name: t
principles: Every deliverable is audited before it counts.
rules: {RULES_PATH}
amendment:
  amendable: [{RULES_PATH}, {COMM_PATH}]
  protected_rules: [submit-validated]
  proposers: "role:lead | role:reviewer"
  voters: "role:lead | role:reviewer"
  pass: {{yes_at_least: 2, no_at_most: 0}}
  voting_window_s: 600
"""
RULES = """
validators:
  tests: {command: "pytest -q", timeout_s: 60}
  acceptance: {command: "{task.acceptance}", timeout_s: 60}
requirements:
  - {name: tasks-have-acceptance, act: task_create, where: {task_is_root: false},
     require: [{field: acceptance}]}
  - {name: submit-validated, act: task_transition, transition: submit,
     require: [{evidence: commit}, {validate: [tests, acceptance]}]}
  - {name: four-eyes, act: task_transition, transition: approve,
     require: [{distinct: [actor, task.assignee]}]}
approvals:
  - {name: release-signoff, act: task_transition, transition: approve,
     where: {task_label: release}, approver: "kind:human"}
facts:
  - name: accepted
    when: {event: task_status_changed, to: done}
    require: [{validated: tests}, {approved_by: "!task.assignee"}]
    revoke_when: {event: task_status_changed, transition: reopen}
  - name: complete
    when: {event: task_status_changed, to: done}
    require: [{children_have: accepted}]
ledger:
  - {name: delivered, when: {fact: accepted}, to: task.assignee, amount: 10}
  - {name: reviewed, when: {fact: accepted}, to: task.reviewer, amount: 3}
  - {name: red-build, when: {validation: failed}, to: act.actor, amount: -2}
  - {name: sent-back, when: {transition: reject}, to: task.assignee, amount: -3}
"""
MEMBERS = [
    Member(id="lead", role="lead"), Member(id="dev", role="worker"),
    Member(id="rev", role="reviewer"), Member(id="owner", kind="human", role="owner"),
]
COMMITS = {"good": "a" * 40, "other": "b" * 40}


def texts(**over: str) -> dict[str, str]:
    return {CONST_PATH: CONST, RULES_PATH: RULES,
            COMM_PATH: (CONFIGS / COMM_PATH).read_text(), **over}


class World:
    def __init__(self, tmp: Path, name: str = "t", **over: str):
        self.clock = FakeClock()
        self.store = EventStore(tmp / f"{name}.db", clock=self.clock)
        self.rs = RuleSet(CONST_PATH, COMM_PATH, texts(**over))
        self.inst = InstitutionView(CONST_PATH, COMM_PATH)
        gate = InstitutionGate(self.inst, lambda ref: COMMITS.get(ref))
        self.comm = CommService(self.store, self.rs.comm, self.clock, extra_gate=gate)
        self.svc = InstitutionService(self.comm, self.inst)
        for m in MEMBERS:
            self.comm.register_member(m)
        self.svc.start(self.rs)

    def act(self, actor: str, kind: str, **params):
        r = self.comm.submit(Act(act_id=new_id("act"), actor=actor, kind=kind, params=params))
        self.svc.settle()
        return r

    def finish_validations(self, ok: bool = True, only: str | None = None) -> None:
        for v in list(self.inst.validations.values()):
            if v.status == "requested" and (only is None or v.validator == only):
                self.svc.record_validation(v.id, ok=ok, exit_code=0 if ok else 1, wall_s=1.0,
                                           output_blob=None, tail="" if ok else "1 failed")
        self.svc.settle()

    def task(self, tid: str):
        return self.comm.view.tasks[tid]

    def types(self) -> list[str]:
        return [e.type for e in self.store.read_global()]

    def started_task(self, reviewer: str | None = "rev", labels=()) -> str:
        r = self.act("lead", "task_create", title="root")
        assert r.ok, r.decision
        r = self.act("lead", "task_create", title="impl", parent=r.data["task"],
                     assignee="dev", reviewer=reviewer, acceptance="python -m pytest t.py",
                     labels=list(labels))
        assert r.ok, r.decision
        tid = r.data["task"]
        assert self.act("dev", "task_transition", task=tid, transition="start").ok
        return tid


# ---- rule set -------------------------------------------------------------------------------

def test_version_changes_with_any_file():
    a, b = RuleSet(CONST_PATH, COMM_PATH, texts()), RuleSet(CONST_PATH, COMM_PATH, texts())
    assert a.version == b.version and len(a.version) == 12
    c = a.with_file(RULES_PATH, RULES + "\n# comment\n")
    assert c.version != a.version


def test_condition_vocabulary_is_closed_and_placed():
    with pytest.raises(ValidationError):
        RulesFile.model_validate({"requirements": [
            {"name": "x", "act": "task_create", "require": [{"field": "acceptance",
                                                              "evidence": "commit"}]}]})
    with pytest.raises(ValidationError, match="cannot be used here"):
        RulesFile.model_validate({"requirements": [
            {"name": "x", "act": "task_create", "require": [{"approved_by": "*"}]}]})
    with pytest.raises(ValidationError, match="unknown validator"):
        RulesFile.model_validate({"requirements": [
            {"name": "x", "act": "task_create", "require": [{"validate": ["nope"]}]}]})
    with pytest.raises(ValidationError):
        RulesFile.model_validate({"requirements": [
            {"name": "x", "act": "task_create", "require": [{"magic": 1}]}]})


# ---- requirements and pending ------------------------------------------------------------

def test_requirement_denies_with_rule_name(tmp_path):
    w = World(tmp_path)
    root = w.act("lead", "task_create", title="root")
    assert root.ok  # where: task_is_root false exempts roots
    r = w.act("lead", "task_create", title="child", parent=root.data["task"])
    assert r.decision.decision == "deny" and r.decision.rule == "tasks-have-acceptance"


def test_submit_needs_commit_then_validates(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    r = w.act("dev", "task_transition", task=tid, transition="submit")
    assert r.decision.rule == "submit-validated" and "commit" in r.decision.reason
    r = w.act("dev", "task_transition", task=tid, transition="submit", commit="nope")
    assert r.decision.decision == "deny"
    r = w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    assert r.decision.decision == "pending"
    assert w.task(tid).status == "in_progress"            # no domain event while pending
    reqs = {v.validator: v for v in w.inst.validations.values()}
    assert reqs["acceptance"].command == "python -m pytest t.py"
    assert reqs["tests"].commit == COMMITS["good"]
    again = w.act("dev", "task_transition", task=tid, transition="submit", commit="other")
    assert again.decision.rule == "pending"                # one waiting transition per task
    w.finish_validations()
    assert w.task(tid).status == "in_review"
    resolved = [e for e in w.store.read("gate") if e.type == "act_resolved"]
    assert resolved[-1].payload["decision"] == "allow"
    status = [e for e in w.store.read_global() if e.type == "task_status_changed"][-1]
    assert status.causation_id == resolved[-1].seq


def test_failed_validation_denies_and_debits(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations(ok=False, only="tests")
    assert w.task(tid).status == "in_progress"
    res = [e for e in w.store.read("gate") if e.type == "act_resolved"][-1].payload
    assert res["decision"] == "deny" and "1 failed" in res["reason"]
    assert w.inst.totals["dev"] == -2
    assert not w.inst.pending


def test_cancel_while_pending_denies_on_resolution(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    assert w.act("lead", "task_transition", task=tid, transition="cancel", note="scope").ok
    assert not w.inst.pending
    res = [e for e in w.store.read("gate") if e.type == "act_resolved"][-1].payload
    assert res["decision"] == "deny"


def test_four_eyes(tmp_path):
    w = World(tmp_path)
    tid = w.started_task(reviewer="dev")
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    r = w.act("dev", "task_transition", task=tid, transition="approve")
    assert r.decision.rule == "four-eyes"


# ---- facts and ledger --------------------------------------------------------------------

def test_accepted_fact_and_ledger(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    assert w.act("rev", "task_transition", task=tid, transition="approve").ok
    assert ("accepted", tid) in w.inst.facts
    assert dict(w.inst.totals) == {"dev": 10, "rev": 3}
    est = next(e for e in w.store.read_global() if e.type == "fact_established")
    trigger = next(e for e in w.store.read_global() if e.type == "task_status_changed" and
                   e.payload["to"] == "done")
    assert est.causation_id == trigger.seq
    assert not w.inst.owed
    w.act("lead", "task_transition", task=tid, transition="reopen", note="regression")
    assert ("accepted", tid) not in w.inst.facts
    assert w.inst.totals["dev"] == 10                       # the ledger is not rewritten


def test_self_approved_done_is_not_accepted(tmp_path):
    w = World(tmp_path)
    tid = w.started_task(reviewer=None)
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    assert w.task(tid).status == "done"
    assert ("accepted", tid) not in w.inst.facts            # approved_by !task.assignee fails
    assert not w.inst.totals


def test_children_have(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    root = w.task(tid).parent
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    w.act("rev", "task_transition", task=tid, transition="approve")
    assert w.act("lead", "task_update", task=root, acceptance="true").ok
    assert w.act("lead", "task_transition", task=root, transition="assign", to="lead").ok
    w.act("lead", "task_transition", task=root, transition="start")
    w.act("lead", "task_transition", task=root, transition="submit", commit="good")
    w.finish_validations()
    assert w.task(root).status == "done"
    assert ("complete", root) in w.inst.facts


def test_rejection_is_debited(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    assert w.act("rev", "task_transition", task=tid, transition="reject", note="no docs").ok
    assert w.inst.totals["dev"] == -3


# ---- human approval ------------------------------------------------------------------------

def test_human_signoff(tmp_path):
    w = World(tmp_path)
    tid = w.started_task(labels=["release"])
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    r = w.act("rev", "task_transition", task=tid, transition="approve")
    assert r.decision.decision == "pending" and r.decision.rule == "release-signoff"
    assert w.task(tid).status == "in_review"
    denied = w.act("rev", "decide_pending", id=r.act_id, decision="approve")
    assert denied.decision.decision == "deny"
    assert w.task(tid).status == "in_review"
    assert w.act("owner", "decide_pending", id=r.act_id, decision="approve").ok
    assert w.task(tid).status == "done"


def test_human_rejects(tmp_path):
    w = World(tmp_path)
    tid = w.started_task(labels=["release"])
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    r = w.act("rev", "task_transition", task=tid, transition="approve")
    assert not w.act("owner", "decide_pending", id=r.act_id, decision="reject").ok  # needs note
    assert w.act("owner", "decide_pending", id=r.act_id, decision="reject", note="later").ok
    assert w.task(tid).status == "in_review"
    res = [e for e in w.store.read("gate") if e.type == "act_resolved"][-1].payload
    assert res["decision"] == "deny" and res["rule"] == "release-signoff"


# ---- amendments ---------------------------------------------------------------------------

LOOSER = RULES.replace("require: [{evidence: commit}, {validate: [tests, acceptance]}]",
                       "require: [{evidence: commit}, {validate: [tests]}]")
ACCEPTANCE_RULE = """  - {name: tasks-have-acceptance, act: task_create, where: {task_is_root: false},
     require: [{field: acceptance}]}
"""
NO_ACCEPTANCE = RULES.replace(ACCEPTANCE_RULE, "")


def test_protected_rule_cannot_change(tmp_path):
    w = World(tmp_path)
    r = w.act("lead", "propose_amendment", path=RULES_PATH, content=LOOSER)
    assert r.decision.rule == "protected_rules"
    assert w.act("dev", "propose_amendment", path=RULES_PATH,
                 content=NO_ACCEPTANCE).decision.decision == "deny"     # not a proposer
    assert w.act("lead", "propose_amendment", path=CONST_PATH,
                 content=CONST).decision.decision == "deny"             # not amendable
    hollow = RULES.replace('tests: {command: "pytest -q"', 'tests: {command: "true"')
    assert w.act("lead", "propose_amendment", path=RULES_PATH,
                 content=hollow).decision.rule == "protected_rules"
    bad = w.act("lead", "propose_amendment", path=RULES_PATH, content="requirements: 3")
    assert bad.decision.rule == "schema"


def test_amendment_enacted_and_new_version_applies(tmp_path):
    w = World(tmp_path)
    v0 = w.inst.rules.version
    root = w.act("lead", "task_create", title="root").data["task"]
    old = w.act("lead", "task_create", title="c", parent=root)
    assert old.decision.decision == "deny"
    r = w.act("lead", "propose_amendment", path=RULES_PATH, content=NO_ACCEPTANCE,
              rationale="acceptance commands slow us down")
    assert r.ok and "-  - {name: tasks-have-acceptance" in r.data["diff"]
    assert w.inst.amendments["A1"].status == "open"          # 1 of 2 yes
    assert w.act("rev", "vote", amendment="A1", choice="yes").ok
    assert w.inst.amendments["A1"].status == "enacted"
    v1 = w.inst.rules.version
    assert v1 != v0
    new = w.act("lead", "task_create", title="c", parent=root)
    assert new.ok
    decided = {e.payload["act_id"]: e for e in w.store.read("gate") if e.type == "act_decided"}
    assert decided[old.act_id].rule_version == v0 and decided[new.act_id].rule_version == v1


def test_vote_change_expiry_and_conflict(tmp_path):
    w = World(tmp_path)
    w.act("lead", "propose_amendment", path=RULES_PATH, content=NO_ACCEPTANCE)
    w.act("rev", "vote", amendment="A1", choice="no")
    assert w.inst.amendments["A1"].status == "open"
    assert w.act("dev", "vote", amendment="A1", choice="yes").decision.decision == "deny"
    w.clock.advance(700)
    w.svc.settle()
    assert w.inst.amendments["A1"].status == "closed"
    assert w.inst.amendments["A1"].reason == "expired"
    # two proposals on the same base: the second to pass conflicts
    w.act("lead", "propose_amendment", path=RULES_PATH, content=NO_ACCEPTANCE)
    w.act("rev", "propose_amendment", path=RULES_PATH, content=RULES + "\n# v2\n")
    w.act("lead", "vote", amendment="A3", choice="yes")
    assert w.inst.amendments["A3"].status == "enacted"
    w.act("rev", "vote", amendment="A2", choice="yes")
    assert w.inst.amendments["A2"].status == "closed"
    assert w.inst.amendments["A2"].reason == "conflict"


def test_comm_policy_amendment_changes_enforcement(tmp_path):
    w = World(tmp_path)
    assert w.act("dev", "post_message", to="@rev", text="hi").ok
    flat = (CONFIGS / COMM_PATH).read_text()
    no_dm = flat.replace('  - {name: dm, act: post_message, via: dm, from: "*", to: "*"}\n', "")
    assert no_dm != flat
    w.act("lead", "propose_amendment", path=COMM_PATH, content=no_dm)
    w.act("rev", "vote", amendment="A1", choice="yes")
    assert w.inst.amendments["A1"].status == "enacted"
    assert w.act("dev", "post_message", to="@rev", text="hi").decision.decision == "deny"


def test_amendments_passing_together_all_take_effect(tmp_path):
    w = World(tmp_path)
    flat = (CONFIGS / COMM_PATH).read_text()
    no_dm = flat.replace('  - {name: dm, act: post_message, via: dm, from: "*", to: "*"}\n', "")
    w.act("lead", "propose_amendment", path=COMM_PATH, content=no_dm)
    w.act("lead", "propose_amendment", path=RULES_PATH, content=NO_ACCEPTANCE)
    for aid in ("A1", "A2"):                     # both pass in the same settle
        w.comm.submit(Act(act_id=new_id("act"), actor="rev", kind="vote",
                          params={"amendment": aid, "choice": "yes"}))
    w.svc.settle()
    assert [w.inst.amendments[a].status for a in ("A1", "A2")] == ["enacted", "enacted"]
    rs = w.inst.rules
    assert rs.texts[COMM_PATH] == no_dm and rs.texts[RULES_PATH] == NO_ACCEPTANCE
    assert w.act("dev", "post_message", to="@rev", text="hi").decision.decision == "deny"


# ---- replay ------------------------------------------------------------------------------

def test_fold_rebuilds_same_state(tmp_path):
    w = World(tmp_path)
    tid = w.started_task()
    w.act("dev", "task_transition", task=tid, transition="submit", commit="good")
    w.finish_validations()
    w.act("rev", "task_transition", task=tid, transition="approve")
    w.act("lead", "propose_amendment", path=RULES_PATH, content=NO_ACCEPTANCE)
    w.act("rev", "vote", amendment="A1", choice="yes")
    fresh = InstitutionView(CONST_PATH, COMM_PATH)
    for ev in w.store.read_global():
        fresh.apply(ev)
    assert fresh.facts == w.inst.facts and dict(fresh.totals) == dict(w.inst.totals)
    assert [v for v, _ in [(r.version, s) for s, r in fresh.versions]] == \
        [r.version for _, r in w.inst.versions]
    assert not fresh.owed


def test_vocabulary_text_covers_the_schema():
    from sciteam.institution.model import (
        FACT_KEYS,
        REQUIREMENT_KEYS,
        ROLE_REFS,
        TASK_FIELDS,
        VOCABULARY,
        Condition,
        RulesFile,
    )

    for key in {*Condition.model_fields, *RulesFile.model_fields}:
        alias = Condition.model_fields[key].alias if key in Condition.model_fields else None
        assert f"{alias or key}" in VOCABULARY
    for word in (*REQUIREMENT_KEYS, *FACT_KEYS, *ROLE_REFS, *TASK_FIELDS):
        assert word in VOCABULARY


def test_validate_without_commit_is_denied_not_requested(tmp_path):
    rules = RULES.replace(
        "require: [{distinct: [actor, task.assignee]}]}",
        "require: [{distinct: [actor, task.assignee]}, {validate: [tests]}]}")
    w = World(tmp_path, **{RULES_PATH: rules})
    tid = w.started_task()
    assert w.act("dev", "task_transition", task=tid, transition="submit", commit="good").ok is False
    w.finish_validations()
    assert w.task(tid).status == "in_review"
    before = len(w.inst.validations)
    r = w.act("rev", "task_transition", task=tid, transition="approve")
    assert not r.ok and "commit=" in r.decision.reason
    assert len(w.inst.validations) == before
