"""Layer 4 through the scheduler: genesis → ratified manifest → hire → certificate → goal →
run stops with goal_met (docs/50-organization.md §3–§7). Scripted LLMs, local sandbox."""

import ast
import asyncio
import shutil
from pathlib import Path

import yaml

from sciteam.institution.team import decide
from sciteam.org.team import OrgTeamRun, open_run
from sciteam.providers.llm.fakes import TEST_MODEL
from sciteam.runtime.team import create_run, run_team
from tests.scenarios.test_team_runtime import FnLlm, fin, tc

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "sciteam"


def setup(tmp: Path) -> Path:
    cfg = tmp / "configs"
    shutil.copytree(REPO / "configs", cfg)
    h = yaml.safe_load((cfg / "harness/team-org.yaml").read_text())
    h.update(model="scripted", sandbox={"kind": "local"},
             recovery={"empty_turn_retries": 1, "provider_retries": 0})
    (cfg / "harness/team-org.yaml").write_text(yaml.safe_dump(h))
    rt = yaml.safe_load((cfg / "runtime/default.yaml").read_text())
    rt.update(debounce_s=0, sandbox={"kind": "local"}, deliver_during_activation=False)
    (cfg / "runtime/default.yaml").write_text(yaml.safe_dump(rt))
    rules = yaml.safe_load((cfg / "institution/genesis-rules.yaml").read_text())
    for probe in ("env-python", "env-pytest"):
        rules["validators"][probe]["command"] = "true"
    (cfg / "institution/genesis-rules.yaml").write_text(yaml.safe_dump(rules))
    run = tmp / "run"
    create_run(run, team="teams/genesis-minipkg.yaml", configs_dir=cfg, opener=OrgTeamRun)
    return run


async def go(run: Path, llms: dict[str, FnLlm]) -> str:
    def factory(agent, info):
        return llms.setdefault(agent, FnLlm(loop=fin()))
    return await asyncio.wait_for(run_team(run, opener=open_run, llm_factory=factory,
                                           models={"scripted": TEST_MODEL}), 60)


def pending_id(run: Path, kind: str) -> str:
    tr = open_run(run, models={})
    try:
        tr.settle()
        return next(k for k, a in tr.inst.approvals.items() if a.status == "requested"
                    and (kind in a.summary))
    finally:
        tr.close()


def test_genesis_to_goal_met(tmp_path):
    run = setup(tmp_path)
    manifest = (REPO / "configs/org/templates/flat/manifest.yaml").read_text() \
        .replace("<the mission agreed with the owner>", "Deliver confkit.")
    llms = {
        "founder": FnLlm(steps=[
            tc("propose_amendment", path="org/genesis.yaml", content=manifest,
               rationale="flat organization for a small package"), fin(),
            tc("hire", seat="member", member="dev-a", template="developer",
               profile="You implement confkit."),
            tc("hire", seat="engineer", member="env-eng", template="engineer"), fin()],
            loop=fin()),
        "dev-a": FnLlm(steps=[
            tc("certify_environment", env="workspace", commit="HEAD"),   # not its seat
            tc("task_create", title="deliver confkit", description="the goal",
               labels=["goal"], acceptance="true", assignee="dev-a"), fin()], loop=fin()),
        "env-eng": FnLlm(steps=[tc("certify_environment", env="workspace", commit="HEAD"),
                                fin()], loop=fin()),
    }
    assert asyncio.run(go(run, llms)) == "quiescent"
    assert decide(run, as_="owner", id=pending_id(run, "org/genesis.yaml"),
                  decision="approve", opener=open_run).startswith("approve")
    assert asyncio.run(go(run, llms)) == "quiescent"
    decide(run, as_="owner", id=pending_id(run, "task_create"), decision="approve",
           opener=open_run)

    tr = open_run(run, models={})
    try:
        goal = tr.org.goals()[0].id
    finally:
        tr.close()
    llms["dev-a"].steps = [tc("task_transition", task=goal, transition="start"),
                           tc("task_transition", task=goal, transition="submit",
                              commit="HEAD"), fin()]
    llms["dev-a"].loop = fin()
    assert asyncio.run(go(run, llms)) == "goal_met"

    tr = open_run(run, models={})
    try:
        types = [e.type for e in tr.store.read_global()]
        assert {"org_enacted", "environment_certified", "goal_met"} <= set(types)
        dev = tr.comm.view.members["dev-a"]
        assert dev.role == "member" and dev.profile == "You implement confkit."
        assert tr.inst.has_fact("accepted", goal)
    finally:
        tr.close()
    # the new member was activated with its seat and charter in the briefing
    first = " ".join(str(m.content) for m in llms["dev-a"].requests[0])
    assert "Your seat: member" in first and "Deliver confkit." in first
    assert "You implement confkit." in first     # its adapted profile is its role prompt
    tr = open_run(run, models={})
    try:
        denied = [e.payload for e in tr.store.read_global(types={"act_decided"})
                  if e.payload["kind"] == "certify_environment"]
        assert [d["decision"] for d in denied] == ["deny", "pending"]
    finally:
        tr.close()


def test_lower_layers_do_not_import_org():
    layers = ("kernel", "providers", "harness", "comm", "runtime", "institution", "org")
    for pkg in layers:
        above = ("sciteam.experiment",) + (() if pkg == "org" else ("sciteam.org",))
        for path in (SRC / pkg).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                assert not any(m.startswith(above) for m in mods), path
    for path in (SRC / "experiment").rglob("*.py"):              # bench/ stays outside
        assert "import bench" not in path.read_text() and "from bench" not in path.read_text()
