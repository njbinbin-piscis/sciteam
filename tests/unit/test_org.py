"""Layer 4 invariants on the real genesis configs (docs/50-organization.md §11)."""

from pathlib import Path

import pytest
import yaml

from sciteam.comm import Act, CommService, Member
from sciteam.comm.policy import SelCtx, compile_selector
from sciteam.institution.gate import InstitutionGate
from sciteam.institution.service import InstitutionService
from sciteam.institution.team import load_ruleset
from sciteam.institution.view import InstitutionView
from sciteam.kernel import EventStore, FakeClock
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.ids import new_id
from sciteam.org.service import OrgService
from sciteam.runtime.config import TeamFile

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
TEAM = "teams/genesis-minipkg.yaml"
MANIFEST, COMM, RULES = "org/genesis.yaml", "comm/genesis.yaml", "institution/genesis-rules.yaml"
MODES = ["hierarchical", "flat", "workflow", "wave", "market"]
COMMIT = "c" * 40


class Org:
    def __init__(self, tmp: Path):
        self.cs = ConfigStore(CONFIGS)
        tf = self.cs.load(TEAM, TeamFile)
        rs = load_ruleset(self.cs, tf).with_file(MANIFEST, self.cs.load_text(MANIFEST))
        self.clock = FakeClock()
        self.store = EventStore(tmp / "org.db", clock=self.clock)
        self.inst = InstitutionView(tf.institution, tf.comm_policy)
        gate = InstitutionGate(self.inst, lambda ref: COMMIT if ref in ("HEAD", COMMIT) else None)
        self.comm = CommService(self.store, rs.comm, self.clock, extra_gate=gate)
        self.isvc = InstitutionService(self.comm, self.inst)
        self.org = OrgService(self.comm, self.isvc, self.cs, MANIFEST)
        self.comm.register_member(Member(id="founder", role="founder",
                                         harness="harness/team-org.yaml"))
        self.comm.register_member(Member(id="owner", kind="human", role="owner"))
        self.isvc.start(rs)
        self.settle()

    def settle(self) -> None:
        for _ in range(5):
            self.isvc.settle()
            if not self.org.settle():
                break

    def act(self, actor: str, kind: str, **params):
        r = self.comm.submit(Act(act_id=new_id("act"), actor=actor, kind=kind, params=params))
        self.settle()
        return r

    def validations(self, ok: bool = True) -> None:
        for v in list(self.inst.validations.values()):
            if v.status == "requested":
                self.isvc.record_validation(v.id, ok=ok, exit_code=0 if ok else 1, wall_s=1.0,
                                            output_blob=None, tail="" if ok else "failed")
        self.settle()

    def adopt(self, mode: str, ratify: bool = True) -> str:
        """Founder proposes the template's manifest and policy; the owner ratifies."""
        for path, f in ((COMM, "comm.yaml"), (MANIFEST, "manifest.yaml")):
            text = self.cs.load_text(f"org/templates/{mode}/{f}")
            if path == MANIFEST:
                text = text.replace("<the mission agreed with the owner>", "Deliver confkit.")
            r = self.act("founder", "propose_amendment", path=path, content=text,
                         rationale=f"adopt {mode}")
            assert r.ok, r.decision
            aid = r.data["amendment"]
            if path == MANIFEST and ratify:
                assert self.act("owner", "decide_pending", id=aid, decision="approve").ok
        return aid

    def hire(self, seat: str, member: str, template: str = "developer", actor="founder", **kw):
        return self.act(actor, "hire", seat=seat, member=member, template=template, **kw)

    def types(self) -> list[str]:
        return [e.type for e in self.store.read_global()]


# ---- O1 ----------------------------------------------------------------------------------

def test_unratified_manifest_has_no_effect(tmp_path):
    o = Org(tmp_path)
    aid = o.adopt("hierarchical", ratify=False)
    assert o.inst.amendments[aid].status == "ratifying"
    r = o.hire("hr", "hr-ana", template="hr")
    assert not r.ok and "no seat 'hr'" in r.decision.reason
    assert o.act("owner", "decide_pending", id=aid, decision="approve").ok
    assert o.hire("hr", "hr-ana", template="hr").ok
    enacted = [e.payload for e in o.store.read_global(types={"org_enacted"})]
    assert enacted[-1]["vacancies"] == {"hr": 1, "engineer": 1, "lead": 1, "developer": 3,
                                        "reviewer": 1}


def test_manifest_cannot_drop_a_held_seat(tmp_path):
    o = Org(tmp_path)
    o.adopt("flat")
    assert o.hire("member", "dev-a").ok
    text = yaml.safe_load(o.inst.rules.texts[MANIFEST])
    text["seats"] = [s for s in text["seats"] if s["name"] != "member"]
    r = o.act("founder", "propose_amendment", path=MANIFEST, content=yaml.safe_dump(text),
              rationale="drop members")
    assert not r.ok and "held by @dev-a" in r.decision.reason


# ---- O2 ----------------------------------------------------------------------------------

def test_hire_checks_seat_template_harness_and_policy(tmp_path):
    o = Org(tmp_path)
    o.adopt("hierarchical")
    assert "unknown template" in o.hire("developer", "d1", template="nope").decision.reason
    assert "allows harness" in o.hire("developer", "d1", harness="harness/team.yaml") \
        .decision.reason
    for i in range(3):
        assert o.hire("developer", f"d{i}").ok
    assert "is full" in o.hire("developer", "d9").decision.reason
    assert "already taken" in o.hire("reviewer", "d0", template="reviewer").decision.reason
    # only group:staffing may hire (communication policy, not code)
    r = o.hire("reviewer", "rev", template="reviewer", actor="d0")
    assert not r.ok and "may not hire" in r.decision.reason


def test_adapted_profile_is_recorded_with_its_template(tmp_path):
    o = Org(tmp_path)
    o.adopt("hierarchical")
    assert o.hire("lead", "lead-li", template="lead").ok
    r = o.hire("developer", "parser-dev", profile="You implement the confkit parser.",
               note="parser specialist")
    assert r.ok and "+You implement the confkit parser." in r.data["diff"]
    reg = [e.payload for e in o.store.read_global(types={"member_registered"})
           if e.payload.get("seat") == "developer"][0]
    assert reg["template"] == "developer" and reg["adapted"] and reg["hired_by"] == "founder"
    m = o.comm.view.members["parser-dev"]
    assert (m.role, m.team, m.manager, m.profile) == \
        ("developer", "core", "lead-li", "You implement the confkit parser.")
    assert "room:team-core" in o.comm.view.convs and \
        "parser-dev" in o.comm.view.convs["room:team-core"].members


def test_groups_follow_the_manifest_and_grant_rights(tmp_path):
    o = Org(tmp_path)
    o.adopt("hierarchical")
    assert o.hire("hr", "hr-ana", template="hr").ok
    ctx = SelCtx(view=o.comm.view, actor="hr-ana")
    assert compile_selector("group:staffing")("hr-ana", ctx)
    assert not compile_selector("group:legislature")("hr-ana", ctx)
    assert o.hire("engineer", "env-eng", template="engineer", actor="hr-ana").ok
    assert o.comm.view.members["env-eng"].manager == "founder"


def test_dismiss_returns_tasks_and_stops_the_member(tmp_path):
    o = Org(tmp_path)
    o.adopt("flat")
    assert o.hire("member", "dev-a").ok
    t = o.act("founder", "task_create", title="x", assignee="dev-a", acceptance="true").data["task"]
    assert not o.act("dev-a", "dismiss", member="founder", reason="coup").ok
    assert o.act("founder", "dismiss", member="dev-a", reason="left").ok
    assert "dev-a" not in o.comm.view.members
    assert o.comm.view.tasks[t].status == "open" and o.comm.view.tasks[t].assignee is None
    assert not o.hire("member", "dev-a").ok  # ids are never reused


# ---- O3 ----------------------------------------------------------------------------------

def test_no_certificate_no_work(tmp_path):
    o = Org(tmp_path)
    t = o.act("founder", "task_create", title="x", assignee="founder", acceptance="true").data["task"]
    r = o.act("founder", "task_transition", task=t, transition="start")
    assert not r.ok and r.decision.rule == "work-needs-environment"
    r = o.act("founder", "certify_environment", env="workspace", commit="HEAD")
    assert r.decision.decision == "pending"
    o.validations(ok=False)
    assert "workspace" not in o.inst.certificates
    r = o.act("founder", "certify_environment", env="workspace", commit="HEAD")
    o.validations(ok=True)
    assert o.inst.certificate("workspace")[0]
    assert o.act("founder", "task_transition", task=t, transition="start").ok


def test_changing_a_probe_voids_the_certificate(tmp_path):
    o = Org(tmp_path)
    o.act("founder", "certify_environment", env="workspace", commit="HEAD")
    o.validations()
    assert o.inst.certificate("workspace")[0]
    rules = yaml.safe_load(o.inst.rules.texts[RULES])
    rules["validators"]["env-pytest"]["command"] = "python -m pytest --version && ruff --version"
    r = o.act("founder", "propose_amendment", path=RULES, content=yaml.safe_dump(rules),
              rationale="need ruff")
    assert r.ok and o.inst.amendments[r.data["amendment"]].status == "enacted"
    ok, why = o.inst.certificate("workspace")
    assert not ok and "no longer matches" in why


def test_only_engineering_certifies(tmp_path):
    o = Org(tmp_path)
    o.adopt("flat")
    assert o.hire("member", "dev-a").ok
    r = o.act("dev-a", "certify_environment", env="workspace", commit="HEAD")
    assert not r.ok and "may not certify_environment" in r.decision.reason


# ---- O4 ----------------------------------------------------------------------------------

def _certified(o: Org) -> None:
    o.act("founder", "certify_environment", env="workspace", commit="HEAD")
    o.validations()


def test_goal_needs_predicate_and_owner_signoff(tmp_path):
    o = Org(tmp_path)
    r = o.act("founder", "task_create", title="deliver", labels=["goal"])
    assert not r.ok and r.decision.rule == "goal-has-predicate"
    r = o.act("founder", "task_create", title="deliver", labels=["goal"],
              acceptance="pytest -q", assignee="founder")
    assert r.decision.decision == "pending" and not o.comm.view.tasks
    assert o.act("owner", "decide_pending", id=r.act_id, decision="approve").ok
    assert len(o.org.goals()) == 1


def test_goal_closes_only_when_its_predicate_passes_and_then_the_run_stops(tmp_path):
    o = Org(tmp_path)
    _certified(o)
    r = o.act("founder", "task_create", title="deliver", labels=["goal"],
              acceptance="pytest -q", assignee="founder")
    o.act("owner", "decide_pending", id=r.act_id, decision="approve")
    g = o.org.goals()[0].id
    assert o.act("founder", "task_transition", task=g, transition="start").ok
    o.act("founder", "task_transition", task=g, transition="submit", commit="HEAD")
    o.validations(ok=False)
    assert o.comm.view.tasks[g].status == "in_progress" and o.org.stop_reason() is None
    o.act("founder", "task_transition", task=g, transition="submit", commit="HEAD")
    o.validations(ok=True)
    assert o.comm.view.tasks[g].status == "done"
    assert o.inst.has_fact("accepted", g) and o.org.stop_reason() == "goal_met"
    assert "goal_met" in o.types()


def test_goal_rules_are_protected(tmp_path):
    o = Org(tmp_path)
    rules = yaml.safe_load(o.inst.rules.texts[RULES])
    rules["requirements"] = [r for r in rules["requirements"] if r["name"] != "goal-validated"]
    r = o.act("founder", "propose_amendment", path=RULES, content=yaml.safe_dump(rules),
              rationale="skip")
    assert not r.ok and r.decision.rule == "protected_rules"


# ---- O5 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_each_template_enacts_and_staffs(tmp_path, mode):
    o = Org(tmp_path)
    o.adopt(mode)
    man = o.org.manifest
    assert [s.name for s in man.seats][:3] == ["founder", "hr", "engineer"]
    assert o.hire("hr", "hr-ana", template="hr").ok
    for s in man.seats[2:]:
        for i in range(s.count):
            assert o.hire(s.name, f"{s.name}-{i}", actor="hr-ana").ok, s.name
    assert all(len(o.org.seated(s.name)) == s.count for s in man.seats)
    assert o.comm.policy.name == mode


def test_modes_differ_in_who_may_talk_to_whom(tmp_path):
    allowed = {}
    for mode in ("hierarchical", "workflow", "flat"):
        o = Org(tmp_path / mode)
        o.adopt(mode)
        o.hire("hr", "hr-ana", template="hr")
        workers = [s for s in o.org.manifest.seats if s.count > 1][0]
        o.hire(workers.name, "w1", actor="hr-ana")
        o.hire(workers.name, "w2", actor="hr-ana")
        r = o.act("w1", "post_message", to="@w2", text="hi")
        allowed[mode] = r.ok
    assert allowed == {"hierarchical": False, "workflow": False, "flat": True}
