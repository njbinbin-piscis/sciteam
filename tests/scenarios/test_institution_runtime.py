import ast
import asyncio
from pathlib import Path

import yaml

from sciteam.comm.view import COMM_EVENT_TYPES
from sciteam.institution.team import InstitutionTeamRun, decide, open_run
from sciteam.institution.view import INSTITUTION_EVENT_TYPES, InstitutionView
from sciteam.providers.llm.fakes import TEST_MODEL
from sciteam.runtime.team import create_run, run_team
from tests.scenarios.test_team_runtime import TEAM_TOOLS, FnLlm, fin, tc, write_configs

SRC = Path(__file__).resolve().parents[2] / "sciteam"
INST_TOOLS = ["rules", "propose_amendment", "vote", "withdraw_amendment", "decide_pending",
              "standing", "pending", "validation_output"]
CONST = """
name: t
principles: Work counts once it is validated and reviewed by someone else.
rules: institution/t-rules.yaml
amendment:
  amendable: [institution/t-rules.yaml]
  protected_rules: [submit-validated]
  proposers: "role:lead | role:reviewer"
  voters: "role:lead | role:reviewer"
  pass: {yes_at_least: all}
"""
RULES = """
validators:
  acceptance: {command: "{task.acceptance}", timeout_s: 60}
requirements:
  - {name: submit-validated, act: task_transition, transition: submit,
     require: [{evidence: commit}, {validate: [acceptance]}]}
approvals:
  - {name: release-signoff, act: task_transition, transition: approve,
     where: {task_label: release}, approver: "kind:human"}
facts:
  - name: accepted
    when: {event: task_status_changed, to: done}
    require: [{validated: acceptance}, {approved_by: "!task.assignee"}]
ledger:
  - {name: delivered, when: {fact: accepted}, to: task.assignee, amount: 10}
  - {name: red-build, when: {validation: failed}, to: act.actor, amount: -2}
"""
MEMBERS = [
    {"id": "lead", "role": "lead", "harness": "harness/test.yaml"},
    {"id": "dev", "role": "worker", "harness": "harness/test.yaml"},
    {"id": "rev", "role": "reviewer", "harness": "harness/test.yaml"},
    {"id": "owner", "kind": "human", "role": "owner"},
]
COMMIT = "git add -A && git -c user.email=d@t -c user.name=dev commit -qm work"


def setup(tmp: Path, labels=()) -> Path:
    cfg = write_configs(tmp, members=MEMBERS, runtime={"deliver_during_activation": False},
                        tasks=[{"key": "t", "by": "lead", "title": "write result",
                                "assignee": "dev", "reviewer": "rev", "labels": list(labels),
                                "acceptance": "grep -q ok result.txt"}])
    h = yaml.safe_load((cfg / "harness/test.yaml").read_text())
    h["tools"] = TEAM_TOOLS + INST_TOOLS
    (cfg / "harness/test.yaml").write_text(yaml.safe_dump(h))
    (cfg / "institution").mkdir()
    (cfg / "institution/t.yaml").write_text(CONST)
    (cfg / "institution/t-rules.yaml").write_text(RULES)
    team = yaml.safe_load((cfg / "teams/t.yaml").read_text())
    team["institution"] = "institution/t.yaml"
    (cfg / "teams/t.yaml").write_text(yaml.safe_dump(team))
    run = tmp / "run"
    create_run(run, team="teams/t.yaml", configs_dir=cfg, goal="g", opener=InstitutionTeamRun)
    return run


async def go(run: Path, llms: dict[str, FnLlm], timeout: float = 30) -> str:
    def factory(agent, info):
        return llms.setdefault(agent, FnLlm(loop=fin()))
    return await asyncio.wait_for(run_team(run, opener=open_run, llm_factory=factory,
                                           models={"scripted": TEST_MODEL}), timeout)


def fold(run: Path):
    tr = open_run(run, models={"scripted": TEST_MODEL})
    try:
        evs = list(tr.store.read_global())
        fresh = InstitutionView(tr.team.institution, tr.team.comm_policy)
        for ev in evs:
            fresh.apply(ev)
        return tr.inst, fresh, evs, dict(tr.comm.view.tasks)
    finally:
        tr.close()


def dev_script() -> FnLlm:
    return FnLlm([
        tc("bash", command=f"echo bad > result.txt && {COMMIT}"),
        tc("task_transition", task="T1", transition="start"),
        tc("task_transition", task="T1", transition="submit", commit="HEAD"),
        fin(),
        tc("bash", command=f"echo ok > result.txt && {COMMIT}"),
        tc("task_transition", task="T1", transition="submit", commit="HEAD"),
        fin(),
    ], loop=fin())


def test_audited_delivery_end_to_end(tmp_path):
    run = setup(tmp_path)
    llms = {"dev": dev_script(),
            "rev": FnLlm([tc("task_transition", task="T1", transition="approve"), fin()],
                         loop=fin())}
    assert asyncio.run(go(run, llms)) == "quiescent"
    inst, fresh, evs, tasks = fold(run)
    assert tasks["T1"].status == "done"
    finished = [e.payload for e in evs if e.type == "validation_finished"]
    assert [f["ok"] for f in finished] == [False, True]
    resolved = [e.payload["decision"] for e in evs if e.type == "act_resolved"]
    assert resolved == ["deny", "allow"]
    assert ("accepted", "T1") in inst.facts
    assert dict(inst.totals) == {"dev": 8}
    # the denial reached the developer in the next briefing, with the validator output
    second = llms["dev"].requests[4][-1].content
    assert "was denied" in second and "acceptance" in second
    assert fresh.facts == inst.facts and dict(fresh.totals) == dict(inst.totals)
    assert not fresh.owed and not fresh.pending


def test_release_waits_for_human(tmp_path):
    run = setup(tmp_path, labels=["release"])
    llms = {"dev": FnLlm([tc("bash", command=f"echo ok > result.txt && {COMMIT}"),
                          tc("task_transition", task="T1", transition="start"),
                          tc("task_transition", task="T1", transition="submit", commit="HEAD"),
                          fin()], loop=fin()),
            "rev": FnLlm([tc("task_transition", task="T1", transition="approve"), fin()],
                         loop=fin())}
    assert asyncio.run(go(run, llms)) == "quiescent"
    inst, _, evs, tasks = fold(run)
    assert tasks["T1"].status == "in_review"                  # no domain event before signoff
    [req] = [e.payload for e in evs if e.type == "approval_requested"]
    assert decide(run, as_="rev", id=req["key"], decision="approve").startswith("denied")
    assert decide(run, as_="owner", id=req["key"], decision="approve") == f"approved {req['key']}"
    inst, _, evs, tasks = fold(run)
    assert tasks["T1"].status == "done"


# ---- I1: one enforcement point ------------------------------------------------------------

WRITERS = {SRC / "comm/service.py", SRC / "institution/service.py", SRC / "org/service.py"}
GUARDED = COMM_EVENT_TYPES | INSTITUTION_EVENT_TYPES | {"act_submitted", "act_decided"}


def test_only_the_services_write_domain_and_institution_events():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in WRITERS:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                    node.func.attr in ("append", "record", "add") and \
                    any(isinstance(a, ast.Constant) and a.value in GUARDED for a in node.args):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders
    for tools in (SRC / "comm/tools.py", SRC / "institution/tools.py", SRC / "org/tools.py"):
        text = tools.read_text()
        assert "store." not in text and ".record(" not in text


def test_validation_requested_before_a_restart_still_resolves(tmp_path):
    import subprocess

    from sciteam.comm import Act
    from sciteam.kernel.ids import new_id

    run = setup(tmp_path)
    ws = run / "workspace"
    (ws / "result.txt").write_text("ok\n")
    subprocess.run(COMMIT, shell=True, cwd=ws, check=True)
    tr = open_run(run, models={"scripted": TEST_MODEL})
    try:
        for tr_ in ("start", "submit"):
            r = tr.comm.submit(Act(act_id=new_id("act"), actor="dev", kind="task_transition",
                                   params={"task": "T1", "transition": tr_, "commit": "HEAD"}))
        assert r.decision.decision == "pending"
    finally:
        tr.close()                                   # the process ends before validating
    assert asyncio.run(go(run, {})) == "quiescent"
    inst, _, evs, tasks = fold(run)
    assert [e.payload["ok"] for e in evs if e.type == "validation_finished"] == [True]
    assert tasks["T1"].status in ("in_review", "done")
