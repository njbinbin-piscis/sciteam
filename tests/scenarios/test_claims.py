"""P1: typed acceptance specs and replayed claims (docs/09 §4)."""

import asyncio
from pathlib import Path

import pytest
import yaml

from sciteam.institution.claims import (AcceptanceSpec, ClaimSpec, compare, parse_spec,
                                        preview_spec, read_claim, replay, resolve_pointer,
                                        success_rate)
from sciteam.institution.team import InstitutionTeamRun, decide, open_run
from sciteam.institution.view import InstitutionView
from sciteam.providers.llm.fakes import TEST_MODEL
from sciteam.runtime.team import create_run, run_team
from tests.scenarios.test_team_runtime import TEAM_TOOLS, FnLlm, fin, tc, write_configs

CONST = "name: t\nprinciples: A result counts when it replays.\nrules: institution/t-rules.yaml\n"
RULES = """
validators:
  acceptance: {command: "{task.acceptance}", timeout_s: 60}
requirements:
  - {name: work-validated, act: task_transition, transition: submit,
     require: [{validate: [acceptance]}]}
  - {name: goal-typed, act: task_transition, transition: start,
     where: {task_is_root: true}, require: [{spec_typed: true}]}
  - {name: goal-claims, act: task_transition, transition: submit,
     where: {task_is_root: true}, require: [{claims_cover: true}, {claims_replayed: true}]}
approvals:
  - {name: owner-confirms-spec, act: propose_acceptance, where: {task_is_root: true},
     approver: "kind:human"}
"""
POLICY_EXTRA = [
    {"name": "specify", "act": "propose_acceptance", "from": "task.creator | task.assignee"},
    {"name": "claim", "act": "record_claim", "from": "task.assignee"},
]
MEMBERS = [
    {"id": "lead", "role": "lead", "harness": "harness/test.yaml"},
    {"id": "rev", "role": "reviewer", "harness": "harness/test.yaml"},
    {"id": "owner", "kind": "human", "role": "owner"},
]
SPEC = {"reproduce": "python3 analysis.py",
        "claims": [{"name": "mean", "file": "results.json", "pointer": "/stats/mean",
                    "min": 0, "max": 100}],
        "deliverables": ["analysis.py", "results.json"]}
ANALYSIS = ("import json; xs=[float(l) for l in open('data.txt')]; "
            "json.dump({'stats': {'mean': sum(xs)/len(xs)}}, open('results.json','w'))")
COMMIT = "git add -A && git -c user.email=d@t -c user.name=lead commit -qm work"
INST_TOOLS = ["propose_acceptance", "record_claim", "acceptance", "pending", "decide_pending"]


def setup(tmp: Path) -> Path:
    cfg = write_configs(tmp, members=MEMBERS, runtime={"deliver_during_activation": False},
                        tasks=[{"key": "g", "by": "owner", "title": "estimate the mean",
                                "assignee": "lead", "reviewer": "rev",
                                "acceptance": "test -s results.json"}])
    h = yaml.safe_load((cfg / "harness/test.yaml").read_text())
    h["tools"] = TEAM_TOOLS + INST_TOOLS
    (cfg / "harness/test.yaml").write_text(yaml.safe_dump(h))
    pol = yaml.safe_load((cfg / "comm/flat.yaml").read_text())
    pol["rules"] += POLICY_EXTRA
    (cfg / "comm/flat.yaml").write_text(yaml.safe_dump(pol))
    (cfg / "institution").mkdir()
    (cfg / "institution/t.yaml").write_text(CONST)
    (cfg / "institution/t-rules.yaml").write_text(RULES)
    team = yaml.safe_load((cfg / "teams/t.yaml").read_text())
    team["institution"] = "institution/t.yaml"
    (cfg / "teams/t.yaml").write_text(yaml.safe_dump(team))
    run = tmp / "run"
    create_run(run, team="teams/t.yaml", configs_dir=cfg, goal="g", opener=InstitutionTeamRun)
    return run


async def go(run: Path, llms: dict[str, FnLlm], timeout: float = 60) -> str:
    def factory(agent, info):
        return llms.setdefault(agent, FnLlm(loop=fin()))
    return await asyncio.wait_for(run_team(run, opener=open_run, llm_factory=factory,
                                           models={"scripted": TEST_MODEL}), timeout)


def state(run: Path):
    tr = open_run(run, models={"scripted": TEST_MODEL})
    try:
        tr.settle()
        evs = list(tr.store.read_global())
        fresh = InstitutionView(tr.team.institution, tr.team.comm_policy)
        for ev in evs:
            fresh.apply(ev)
        return tr.inst, fresh, evs, dict(tr.comm.view.tasks)
    finally:
        tr.close()


def errors(llm: FnLlm) -> list[str]:
    """Contents of tool results the scripted member saw, in order."""
    out = []
    for req in llm.requests:
        for m in req:
            if getattr(m, "role", "") == "tool" and m.content not in out:
                out.append(m.content)
    return out


# ---- unit: pointer, comparison, spec schema ---------------------------------------------

def test_pointer_and_compare():
    doc = {"a": [{"b/c": 1.5}], "t~x": 2}
    assert resolve_pointer(doc, "/a/0/b~1c") == 1.5 and resolve_pointer(doc, "/t~0x") == 2
    spec = parse_spec(SPEC)
    c = spec.claims[0]
    assert compare(42.0, 42.0, c, spec) == "ok"
    assert compare(42.0, 43.0, c, spec) == "mismatch"
    assert compare(420.0, 420.0, c, spec) == "out_of_range"
    assert compare("42", 42.0, c, spec) == "not_number"
    assert compare(True, 1.0, c, spec) == "not_number"


def test_read_claim_codes(tmp_path):
    c = ClaimSpec(name="m", file="r.json", pointer="/x")
    assert read_claim(tmp_path, c)[0] == "file_missing"
    (tmp_path / "r.json").write_text("{nope")
    assert read_claim(tmp_path, c)[0] == "not_json"
    (tmp_path / "r.json").write_text('{"y": 1}')
    assert read_claim(tmp_path, c) == ("pointer_missing", "/x: no 'x' at /, which holds keys y")
    (tmp_path / "r.json").write_text('{"mu_max": {"25": 0.68}}')
    assert read_claim(tmp_path, ClaimSpec(name="m", file="r.json", pointer="/mu_max/T25"))[1] \
        == "/mu_max/T25: no 'T25' at /mu_max, which holds keys 25"
    (tmp_path / "r.json").write_text('{"y": 1}')
    (tmp_path / "r.json").write_text('{"x": 3}')
    assert read_claim(tmp_path, c) == ("ok", 3)


def test_preview_reads_each_claim_from_the_current_files():
    spec = parse_spec({"reproduce": "python a.py", "claims": [
        {"name": "lm", "file": "r.json", "pointer": "/logistic/mu_max"},
        {"name": "gm", "file": "r.json", "pointer": "/Gompertz/mu_max"},
        {"name": "best", "file": "r.json", "pointer": "/best_model"},
        {"name": "acc", "file": "out.json", "pointer": "/acc"},
        {"name": "aic", "file": "r.json", "pointer": "/logistic/AIC", "min": 0, "max": 1000}]})
    texts = {"r.json": '{"logistic": {"mu_max": 0.565, "AIC": -377.0}, "gompertz": {}, '
                       '"best_model": "x"}',
             "out.json": None}
    assert preview_spec(spec, texts) == [
        "lm: r.json/logistic/mu_max = 0.565",
        "gm: r.json /Gompertz/mu_max: no 'Gompertz' at /, which holds keys logistic, "
        "gompertz, best_model",
        "best: r.json/best_model holds the value 'x', which is not a number",
        "acc: out.json is not in the workspace yet",
        "aic: r.json/logistic/AIC = -377, outside the range [0, 1000]"]


def test_failed_replay_names_files_the_commit_lacks(tmp_path):
    import subprocess

    from sciteam.providers.sandbox import LocalSandbox
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("1\n3\n")
    subprocess.run(f"git init -q && {COMMIT}", shell=True, cwd=ws, check=True)
    sha = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    (ws / "analysis.py").write_text(ANALYSIS)
    out = asyncio.run(replay(ws, sha, parse_spec(SPEC), [("mean", 2.0)], LocalSandbox))
    assert out["mean"].code == "reproduce_failed"
    assert f"commit {sha[:10]} lacks analysis.py" in out["mean"].tail
    subprocess.run(COMMIT, shell=True, cwd=ws, check=True)
    head = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert asyncio.run(replay(ws, head, parse_spec(SPEC), [("mean", 2.0)],
                              LocalSandbox))["mean"].code == "ok"
    printed = parse_spec({"reproduce": "python3 analysis.py", "claims": [
        {"name": "mean", "file": "stdout", "pointer": "/stats/mean"}]})
    r = asyncio.run(replay(ws, head, printed, [("mean", 2.0)], LocalSandbox))["mean"]
    assert r.code == "file_missing"
    assert "left no file stdout at the repository root; JSON files there: results.json" in r.tail
    readonly = parse_spec({"reproduce": "python3 -c \"raise OSError(30, 'Read-only file system')\"",
                           "claims": [{"name": "mean", "file": "results.json",
                                       "pointer": "/stats/mean"}]})
    r = asyncio.run(replay(ws, head, readonly, [("mean", 2.0)], LocalSandbox))["mean"]
    assert r.code == "reproduce_failed"
    assert "write results.json relative to the current directory" in r.tail


def test_a_committed_result_the_run_does_not_rewrite_fails(tmp_path):
    import subprocess

    from sciteam.providers.sandbox import LocalSandbox
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "results.json").write_text('{"stats": {"mean": 2.0}}')
    elsewhere = ANALYSIS.replace("open('results.json','w')", "open('/dev/null','w')")
    (ws / "analysis.py").write_text(elsewhere)
    (ws / "data.txt").write_text("1\n3\n")
    subprocess.run(f"git init -q && {COMMIT}", shell=True, cwd=ws, check=True)
    sha = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    r = asyncio.run(replay(ws, sha, parse_spec(SPEC), [("mean", 2.0)], LocalSandbox))["mean"]
    assert r.code == "file_missing"
    assert "results.json is in the commit, and the replay removes it" in r.tail


def test_uncommitted_files_the_reproduce_command_names(tmp_path):
    import subprocess

    from sciteam.institution.team import uncommitted_named
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("1\n3\n")
    subprocess.run(f"git init -q && {COMMIT}", shell=True, cwd=ws, check=True)
    sha = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    (ws / "analysis.py").write_text(ANALYSIS)
    (ws / "a.py").write_text("")
    assert uncommitted_named(ws, sha, "python3 analysis.py data.txt") == ["analysis.py"]
    assert uncommitted_named(ws, sha, "python3 /workspace/analysis.py") == ["analysis.py"]
    assert uncommitted_named(ws, sha, "cd . && python3 ./data.py") == []
    assert uncommitted_named(ws, sha, "python3 /private/rev/check.py") == []


def test_spec_schema_rejects_bad_specs():
    for bad in ({"reproduce": "x", "claims": []},
                {"reproduce": "x", "claims": [{"name": "a", "file": "/etc/r", "pointer": "/x"}]},
                {"reproduce": "x", "claims": [{"name": "a", "file": "r", "pointer": "x"}]},
                {"reproduce": "x", "claims": [{"name": "a", "file": "r", "pointer": "/x"},
                                              {"name": "a", "file": "r", "pointer": "/y"}]}):
        with pytest.raises(ValueError):
            AcceptanceSpec.model_validate(bad)
    with pytest.raises(ValueError, match="root of a clean, writable export"):
        ClaimSpec(name="a", file="/private/replicator/results.json", pointer="/x")


# ---- scenario ---------------------------------------------------------------------------

def lead_script(first_value: float) -> FnLlm:
    """Propose the spec, then (after the owner confirms) do the work, record and submit. The
    first claim is recorded before the input changes, so it is stale on the new commit."""
    return FnLlm([
        tc("propose_acceptance", task="T1", spec=SPEC),
        fin(outcome="need_input", summary="spec awaits the owner"),
        tc("task_transition", task="T1", transition="start"),
        tc("bash", command=f"printf '1\\n2\\n3\\n' > data.txt && echo \"{ANALYSIS}\" > "
                           f"analysis.py && python3 analysis.py && {COMMIT}"),
        tc("bash", command=f"printf '1\\n2\\n9\\n' > data.txt && {COMMIT}"),
        tc("record_claim", task="T1", claim="mean", value=first_value, commit="HEAD"),
        tc("task_transition", task="T1", transition="submit", commit="HEAD"),
        fin(outcome="need_input", summary="waiting for the replay"),
        tc("acceptance", task="T1"),
        tc("record_claim", task="T1", claim="mean", value=4.0, commit="HEAD"),
        tc("task_transition", task="T1", transition="submit", commit="HEAD"),
        fin(),
    ], loop=fin())


def test_spec_confirmation_then_stale_claim_denied_then_replayed(tmp_path):
    run = setup(tmp_path)
    lead = lead_script(first_value=2.0)
    asyncio.run(go(run, {"lead": lead}))
    inst, _, evs, tasks = state(run)
    # the spec of a root task waits for the owner; starting needs it in force
    assert "T1" not in inst.specs
    [req] = [e.payload for e in evs if e.type == "approval_requested"]
    assert req["approver"] == "kind:human" and "/stats/mean" in req["summary"]
    assert decide(run, as_="owner", id=req["key"], decision="approve").startswith("approved")

    asyncio.run(go(run, {"lead": lead}))
    inst, fresh, evs, tasks = state(run)
    assert inst.specs["T1"]["by"] == "lead"
    replays = [e.payload for e in evs if e.type == "claim_replayed"]
    assert [r["code"] for r in replays] == ["mismatch", "ok"]
    assert replays[0]["observed"] == 4.0
    seen = "\n".join(str(getattr(m, "content", "") or "") for r in lead.requests for m in r)
    assert "record_claim mean again with value=4.0" in seen
    resolved = [e.payload for e in evs if e.type == "act_resolved"
                and e.payload["kind"] == "task_transition"]
    assert resolved[0]["decision"] == "deny" and resolved[0]["rule"] == "goal-claims"
    assert "did not replay" in resolved[0]["reason"] and "mismatch" in resolved[0]["reason"]
    assert resolved[-1]["decision"] == "allow"
    assert tasks["T1"].status == "in_review"
    assert success_rate(list(inst.claims.values())) == (0, 1)
    assert success_rate(list(inst.claims.values()), first_only=False) == (1, 2)
    assert set(fresh.claims) == set(inst.claims) and fresh.specs.keys() == inst.specs.keys()
    # a denial on another commit points at the commit holding the replayed claims
    from sciteam.institution.gate import InstitutionGate
    head = inst.claims["C2"].commit
    hint = InstitutionGate(inst, lambda ref: head)._elsewhere(tasks["T1"], "0" * 40)
    assert f"[HEAD is {head[:10]}]" in hint
    assert f"your newest claims are on commit {head[:10]}: mean passed" in hint


def test_start_needs_a_spec_and_claims_need_the_spec(tmp_path):
    run = setup(tmp_path)
    lead = FnLlm([tc("task_transition", task="T1", transition="start"),
                  tc("record_claim", task="T1", claim="mean", value=1, commit="HEAD"),
                  tc("propose_acceptance", task="T1", spec={"reproduce": "x", "claims": []}),
                  fin()], loop=fin())
    asyncio.run(go(run, {"lead": lead}))
    seen = errors(lead)
    assert any("no typed acceptance spec" in s for s in seen)
    assert any("has no acceptance spec" in s for s in seen)
    assert any("invalid acceptance spec" in s for s in seen)
