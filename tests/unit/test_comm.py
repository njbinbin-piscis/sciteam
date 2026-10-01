import threading
from pathlib import Path

import pytest

from sciteam.comm import Act, CommPolicy, CommService, CommView, Member
from sciteam.comm.board import cpm
from sciteam.comm.model import Dependency, Task, extract_mentions
from sciteam.comm.policy import SelectorError, compile_selector
from sciteam.kernel import EventStore, FakeClock
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.ids import new_id

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
POLICIES = ("flat", "hierarchical", "workflow", "wave", "market")


def policy(name: str) -> CommPolicy:
    return ConfigStore(CONFIGS).load(f"comm/{name}.yaml", CommPolicy)


MEMBERS = [
    Member(id="lead", role="lead", team="core"),
    Member(id="dev1", role="worker", team="core", manager="lead"),
    Member(id="dev2", role="worker", team="core", manager="lead"),
    Member(id="rev", role="reviewer", team="qa", manager="lead"),
    Member(id="owner", kind="human", role="owner"),
]


def make(tmp_path: Path, name: str = "flat", clock: FakeClock | None = None) -> CommService:
    store = EventStore(tmp_path / f"{name}.db")
    svc = CommService(store, policy(name), clock or FakeClock())
    for m in MEMBERS:
        svc.register_member(m)
    svc.system_room("general", {m.id for m in MEMBERS})
    return svc


def act(svc: CommService, actor: str, kind: str, **params):
    return svc.submit(Act(act_id=new_id("act"), actor=actor, kind=kind, params=params))


def decisions(svc: CommService) -> list[dict]:
    return [e.payload for e in svc.store.read("gate") if e.type == "act_decided"]


# ---- messages, visibility, threads (C1, C2) ------------------------------------------------

def test_dm_is_private(tmp_path):
    svc = make(tmp_path)
    r = act(svc, "dev1", "post_message", to="@dev2", text="hi")
    assert r.ok and r.data["conv"] == "dm:dev1+dev2"
    assert svc.can_read("dev2", "dm:dev1+dev2")
    assert not svc.can_read("lead", "dm:dev1+dev2")
    assert svc.can_read("owner", "dm:dev1+dev2")  # humans_see_all: observer right


def test_room_threads(tmp_path):
    svc = make(tmp_path)
    a = act(svc, "lead", "post_message", to="#general", text="plan?")
    b = act(svc, "dev1", "post_message", to="#general", text="yes", reply_to=a.data["msg_id"])
    c = act(svc, "dev2", "post_message", to="#general", text="+1", reply_to=b.data["msg_id"])
    msgs = svc.view.messages["room:general"]
    assert [m.thread for m in msgs] == [a.data["msg_id"]] * 3
    assert c.ok
    bad = act(svc, "dev2", "post_message", to="#general", text="x", reply_to="m999")
    assert not bad.ok and bad.decision.rule == "structure"


def test_mention_extraction_only_known_members():
    known = {"dev1", "dev2"}
    text = "mail a@dev1.org, @decorator, @dev2, and (@dev1). @human"
    assert extract_mentions(text, known) == ["dev2", "dev1", "human"]


def test_mention_of_nonmember_joins_room_but_not_dm(tmp_path):
    svc = make(tmp_path)
    assert act(svc, "lead", "create_room", name="design", members=["dev1"]).ok
    r = act(svc, "lead", "post_message", to="#design", text="@rev please look")
    assert r.ok and "rev" in svc.view.convs["room:design"].members
    act(svc, "dev1", "post_message", to="@dev2", text="hello")
    r = act(svc, "dev1", "post_message", to="@dev2", text="cc @lead")
    assert not r.ok and "cannot read this private conversation" in r.decision.reason


# ---- policy (C5) ---------------------------------------------------------------------------

def test_hierarchical_reachability(tmp_path):
    svc = make(tmp_path, "hierarchical")
    assert act(svc, "dev1", "post_message", to="@lead", text="status").ok
    assert act(svc, "lead", "post_message", to="@dev2", text="do X").ok
    r = act(svc, "dev1", "post_message", to="@dev2", text="psst")
    assert not r.ok and "no rule allows it" in r.decision.reason
    assert decisions(svc)[-1]["decision"] == "deny"
    assert "dm:dev1+dev2" not in svc.view.convs  # denied acts produce no domain events


def test_one_refused_mention_rejects_whole_message(tmp_path):
    svc = make(tmp_path, "wave")
    svc.system_room("x", {"dev1", "dev2", "lead"})
    r = act(svc, "dev1", "post_message", to="@lead", text="ask @dev2 and @lead")
    assert not r.ok and "@dev2" in r.decision.reason
    assert not svc.view.messages.get("dm:dev1+lead")


def test_rate_limit(tmp_path):
    svc = make(tmp_path)
    lim = svc.policy.limits.messages_per_agent_per_min
    for i in range(lim):
        assert act(svc, "dev1", "post_message", to="#general", text=f"n{i}").ok
    r = act(svc, "dev1", "post_message", to="#general", text="one more")
    assert not r.ok and r.decision.rule == "limits.messages_per_agent_per_min"
    assert act(svc, "owner", "post_message", to="#general", text="humans are exempt").ok


# Expected allow matrix: (actor, act kind, params) -> policies where it is allowed.
MATRIX = [
    (("dev1", "post_message", {"to": "@dev2", "text": "x"}), {"flat", "market"}),
    (("dev1", "post_message", {"to": "@lead", "text": "x"}), {"flat", "market", "hierarchical",
                                                              "wave"}),
    (("dev1", "post_message", {"to": "@owner", "text": "x"}), set(POLICIES)),
    (("dev1", "create_room", {"name": "side", "members": ["dev2"]}), {"flat"}),
    (("dev1", "task_create", {"title": "t", "description": "d"}), {"flat", "market",
                                                                   "hierarchical"}),
    (("lead", "task_create", {"title": "t", "description": "d", "assignee": "dev1"}),
     {"flat", "hierarchical", "wave"}),
    (("dev1", "task_create", {"title": "t", "description": "d", "assignee": "dev2"}),
     {"flat"}),
]


@pytest.mark.parametrize("name", POLICIES)
def test_policy_matrix(tmp_path, name):
    svc = make(tmp_path, name)
    got = {i for i, ((actor, kind, params), _) in enumerate(MATRIX)
           if act(svc, actor, kind, **params).ok}
    want = {i for i, (_, allowed) in enumerate(MATRIX) if name in allowed}
    assert got == want


def test_selector_language():
    view = CommView()
    for m in MEMBERS:
        view.members[m.id] = m
    from sciteam.comm.policy import SelCtx
    ctx = SelCtx(view=view, actor="dev1")
    sel = compile_selector("self.manager | (team:core & !self)")
    assert {x for x in view.members if sel(x, ctx)} == {"lead", "dev2"}
    assert compile_selector("self.peers")("dev2", ctx) and not \
        compile_selector("self.peers")("lead", ctx)
    for bad in ("", "a |", "(a", "role:", "self.boss", "$nope"):
        with pytest.raises(SelectorError):
            compile_selector(bad)


# ---- tasks (C4) ----------------------------------------------------------------------------

def _t(svc, actor="lead", **kw):
    r = act(svc, actor, "task_create", title=kw.pop("title", "t"), description="d", **kw)
    assert r.ok, r.decision
    return r.data["task"]


def tr(svc, actor, task, transition, **kw):
    return act(svc, actor, "task_transition", task=task, transition=transition, **kw)


def test_task_lifecycle_and_illegal_transitions(tmp_path):
    svc = make(tmp_path)
    t = _t(svc, reviewer="rev")
    assert not tr(svc, "dev1", t, "start").ok                    # open: not startable
    assert tr(svc, "lead", t, "assign", to="dev1").ok
    assert not tr(svc, "dev2", t, "start").ok                    # not the assignee
    assert tr(svc, "dev1", t, "start").ok
    assert tr(svc, "dev1", t, "submit", artifacts=["pkg/x.py"], note="done").ok
    assert svc.view.tasks[t].status == "in_review"
    assert not tr(svc, "dev1", t, "approve").ok                  # not the reviewer
    assert not tr(svc, "rev", t, "reject").ok                    # reject needs a note
    assert tr(svc, "rev", t, "reject", note="missing tests").ok
    assert svc.view.tasks[t].status == "in_progress"
    assert tr(svc, "dev1", t, "submit", note="tests added").ok
    assert tr(svc, "rev", t, "approve", note="ok").ok
    task = svc.view.tasks[t]
    assert task.status == "done" and task.artifacts[0].path == "pkg/x.py"
    assert [m.text for m in svc.view.messages[f"task:{t}"]][0] == "[submit] done"
    assert tr(svc, "lead", t, "reopen", note="regression").ok
    assert svc.view.tasks[t].status == "assigned"


def test_submit_without_reviewer_completes(tmp_path):
    svc = make(tmp_path)
    t = _t(svc, assignee="dev1")
    tr(svc, "dev1", t, "start")
    assert tr(svc, "dev1", t, "submit").data["status"] == "done"


@pytest.mark.parametrize("dtype,gate", [("FS", "start"), ("SS", "start"), ("FF", "submit"),
                                         ("SF", "submit")])
def test_dependency_types(tmp_path, dtype, gate):
    svc = make(tmp_path)
    a = _t(svc, assignee="dev1")
    b = _t(svc, assignee="dev2", deps=[{"task": a, "type": dtype}])
    if gate == "start":
        r = tr(svc, "dev2", b, "start")
        assert not r.ok and "unmet dependencies" in r.decision.reason
        tr(svc, "dev1", a, "start")
        assert tr(svc, "dev2", b, "start").ok == (dtype == "SS")
        tr(svc, "dev1", a, "submit")
        assert tr(svc, "dev2", b, "start").ok or dtype == "SS"
    else:
        assert tr(svc, "dev2", b, "start").ok
        r = tr(svc, "dev2", b, "submit")
        assert not r.ok
        tr(svc, "dev1", a, "start")
        assert tr(svc, "dev2", b, "submit").ok == (dtype == "SF")
        if dtype == "FF":
            tr(svc, "dev1", a, "submit")
            assert tr(svc, "dev2", b, "submit").ok


def test_dependency_cycle_rejected(tmp_path):
    svc = make(tmp_path)
    a = _t(svc)
    b = _t(svc, deps=[a])
    c = _t(svc, deps=[b])
    r = act(svc, "lead", "task_update", task=a, deps=[c])
    assert not r.ok and "cycle" in r.decision.reason


def test_cancel_cascades_to_subtasks(tmp_path):
    svc = make(tmp_path)
    p = _t(svc)
    k = _t(svc, parent=p)
    assert tr(svc, "lead", p, "cancel", note="scope cut").ok
    assert svc.view.tasks[k].status == "cancelled"


def test_concurrent_claim_single_winner(tmp_path):
    svc = make(tmp_path, "market")
    t = _t(svc, actor="dev1")
    results = []
    barrier = threading.Barrier(3)

    def claim(who):
        barrier.wait()
        results.append(tr(svc, who, t, "claim").ok)

    threads = [threading.Thread(target=claim, args=(w,)) for w in ("dev1", "dev2", "lead")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == [False, False, True]


def test_cpm_textbook():
    def task(tid, h, *deps):
        return Task(id=tid, short=tid, title=tid, created_by="x", estimate_h=h,
                    deps=[Dependency(task=d) for d in deps])
    tasks = {t.id: t for t in (task("A", 3), task("B", 2, "A"), task("C", 4, "A"),
                               task("D", 1, "B", "C"))}
    rows, end = cpm(tasks)
    by = {r.task: r for r in rows}
    assert end == 8
    assert [r.task for r in rows if r.critical] == ["A", "C", "D"]
    assert by["B"].float_h == 2 and by["B"].es == 3 and by["B"].ls == 5


def test_cpm_ss_and_ff():
    a = Task(id="A", short="A", title="A", created_by="x", estimate_h=4)
    b = Task(id="B", short="B", title="B", created_by="x", estimate_h=2,
             deps=[Dependency(task="A", type="SS")])
    c = Task(id="C", short="C", title="C", created_by="x", estimate_h=1,
             deps=[Dependency(task="A", type="FF")])
    rows, end = cpm({"A": a, "B": b, "C": c})
    by = {r.task: r for r in rows}
    assert end == 4 and by["B"].es == 0 and by["C"].es == 3 and by["C"].critical


# ---- fold consistency ----------------------------------------------------------------------

def test_view_refold_matches_incremental(tmp_path):
    svc = make(tmp_path)
    a = _t(svc, assignee="dev1", reviewer="rev", estimate_h=2)
    _t(svc, deps=[{"task": a, "type": "SS"}], watchers=["dev2"])
    act(svc, "dev1", "post_message", to=a, text="starting @rev")
    tr(svc, "dev1", a, "start")
    act(svc, "dev1", "post_message", to="@dev2", text="hi")
    fresh = CommService(svc.store, svc.policy, FakeClock()).view
    for attr in ("members", "convs", "tasks", "msg_index"):
        assert {k: v.model_dump() for k, v in getattr(fresh, attr).items()} == \
            {k: v.model_dump() for k, v in getattr(svc.view, attr).items()}


def test_cpm_skips_summary_tasks():
    epic = Task(id="E", short="E", title="E", created_by="x", estimate_h=10)
    a = Task(id="A", short="A", title="A", created_by="x", estimate_h=2, parent="E")
    b = Task(id="B", short="B", title="B", created_by="x", estimate_h=3, parent="E",
             deps=[Dependency(task="A")])
    rows, end = cpm({"E": epic, "A": a, "B": b})
    assert end == 5 and [r.task for r in rows] == ["A", "B"]
