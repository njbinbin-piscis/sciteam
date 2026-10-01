"""docs/60-experiment.md §11: specs, overlays, metrics, carry, report (no model calls)."""

import json
from pathlib import Path

import pytest
import yaml

from sciteam.experiment.carry import carry
from sciteam.experiment.metrics import recovered_tasks, run_metrics
from sciteam.experiment.report import render_report, summarize
from sciteam.experiment.runner import RESULT, RunKey, overlays_for, plan, task_overlay
from sciteam.experiment.spec import Arm, TaskSet, load_spec, render
from sciteam.kernel.canonical import sha256_hex
from sciteam.kernel.config_store import ConfigStore, interpreter_digest
from sciteam.kernel.events import EventStore

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def test_interpreter_digest_tracks_sources_only(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n")
    d0 = interpreter_digest(tmp_path)
    (tmp_path / "notes.yaml").write_text("k: v\n")
    assert interpreter_digest(tmp_path) == d0
    (tmp_path / "pkg" / "b.py").write_text("y = 3\n")
    assert interpreter_digest(tmp_path) != d0


def test_snapshot_records_interpreter_digest():
    snap = ConfigStore(CONFIGS).snapshot()
    assert snap.interpreter_digest == interpreter_digest()


def _world(tmp_path: Path) -> Path:
    cfg = tmp_path / "configs"
    (cfg / "teams").mkdir(parents=True)
    (cfg / "runtime").mkdir()
    (cfg / "tasksets").mkdir()
    (cfg / "experiments").mkdir()
    (cfg / "arm-b" / "runtime").mkdir(parents=True)
    (cfg / "teams" / "t.yaml").write_text(yaml.safe_dump({
        "name": "t", "comm_policy": "comm/flat.yaml", "runtime": "runtime/r.yaml",
        "workspace_seed": "{task.seed}",
        "members": [{"id": "a"}, {"id": "owner", "kind": "human"}],
        "kickoff": [{"by": "owner", "to": "@a", "text": "{task.brief}"}]}))
    (cfg / "runtime" / "r.yaml").write_text(yaml.safe_dump(
        {"sandbox": {"kind": "docker", "image": "base", "memory": "8g"}, "debounce_s": 5}))
    (cfg / "arm-b" / "runtime" / "r.yaml").write_text(yaml.safe_dump(
        {"sandbox": {"kind": "docker", "image": "base", "memory": "16g"}, "debounce_s": 5}))
    (cfg / "tasksets" / "ts.yaml").write_text(yaml.safe_dump({
        "name": "ts", "grader": "true {workspace} {out} {task.id}",
        "vars": {"brief": "do it: \"{task.id}\" now"},
        "patches": {"runtime/r.yaml": {"sandbox": {"image": "img-{task.id}"}}},
        "tasks": [{"id": "x", "vars": {"seed": "/abs/x"}}, {"id": "y", "vars": {"seed": "s/y"}}]}))
    (cfg / "experiments" / "e.yaml").write_text(yaml.safe_dump({
        "name": "e", "hypothesis": "h", "primary": "score", "theta": 0.5,
        "tasks": "tasksets/ts.yaml", "repeats": 2, "parallel": 1,
        "arms": [{"name": "a", "team": "teams/t.yaml"},
                 {"name": "b", "team": "teams/t.yaml", "overlay": "arm-b"}],
        "owner": {"mode": "auto-approve", "nudges": 1, "nudge_text": "go", "max_rounds": 5},
        "audit": "true {run} {out}"}))
    return cfg


def test_plan_interleaves_arms_within_a_repeat(tmp_path):
    cfg = _world(tmp_path)
    spec, ts = load_spec(ConfigStore(cfg), "experiments/e.yaml")
    keys = plan(spec, ts)
    assert len(keys) == 2 * 2 * 2
    assert keys[:4] == [RunKey("a", "x", 1), RunKey("b", "x", 1),
                        RunKey("a", "y", 1), RunKey("b", "y", 1)]
    assert {k.r for k in keys[4:]} == {2}


def test_task_overlay_renders_vars_and_patches_the_arm_view(tmp_path):
    cfg = _world(tmp_path)
    spec, ts = load_spec(ConfigStore(cfg), "experiments/e.yaml")
    arm_b = spec.arms[1]
    dest = task_overlay(cfg, arm_b, ts, ts.tasks[0], tmp_path / "ov")
    cs = ConfigStore(cfg, overlays_for(cfg, arm_b, dest))
    team = yaml.safe_load(cs.load_text("teams/t.yaml"))
    assert team["workspace_seed"] == "/abs/x"
    assert team["kickoff"][0]["text"] == 'do it: "x" now'      # quotes survive as data
    rt = yaml.safe_load(cs.load_text("runtime/r.yaml"))
    assert rt["sandbox"] == {"kind": "docker", "image": "img-x", "memory": "16g"}
    assert rt["debounce_s"] == 5


def test_unknown_task_variable_is_an_error():
    with pytest.raises(KeyError):
        render("{task.nope}", {"id": "x"})


def test_only_tasks_must_exist(tmp_path):
    cfg = _world(tmp_path)
    e = yaml.safe_load((cfg / "experiments" / "e.yaml").read_text())
    e["only_tasks"] = ["zz"]
    (cfg / "experiments" / "e.yaml").write_text(yaml.safe_dump(e))
    with pytest.raises(ValueError):
        load_spec(ConfigStore(cfg), "experiments/e.yaml")


def _events(tmp_path: Path, rows: list[tuple[str, str, dict]]) -> list:
    store = EventStore(tmp_path / "events.db")
    for stream, typ, payload in rows:
        store.append(stream, typ, payload, actor="system")
    evs = store.read_global()
    store.close()
    return evs


def test_metrics_closed_stuck_recovered(tmp_path):
    evs = _events(tmp_path, [
        ("run", "run_started", {"config": {"interpreter_digest": "D", "digest": "C"}}),
        ("task/T1", "task_status_changed", {"task_id": "T1", "transition": "reject"}),
        ("task/T2", "task_status_changed", {"task_id": "T2", "transition": "approve"}),
        ("facts", "fact_established", {"fact": "accepted", "subject": "T1"}),
        ("facts", "fact_established", {"fact": "accepted", "subject": "T2"}),
        ("run", "run_stopped", {"reason": "quiescent"}),
        ("run", "run_stopped", {"reason": "goal_met"})])
    assert recovered_tasks(evs) == ["T1"]
    check = {"totals": {"cost_usd": 1.5}, "C4": {"transitions": {"reject": 1}}, "O": {},
             "C5": {"replay": {"decision_or_event_mismatches": 0,
                               "domain_events_without_allowed_act": 0}}}
    m = run_metrics(evs, check, {"score": 0.4, "passed": 4, "total": 10}, {"nudges": 1})
    assert m["closed"] and not m["stuck"] and not m["breaker"]
    assert m["recovered"] == 1 and m["returned"] == 1 and m["owner_nudges"] == 1
    assert m["replay_ok"] and m["interpreter_digest"] == "D" and m["score"] == 0.4
    stuck = run_metrics(evs[:-1], check, None, {})
    assert stuck["stuck"] and not stuck["closed"] and stuck["score"] is None
    no_goals = run_metrics(evs[:-1], {k: v for k, v in check.items() if k != "O"}, None, {})
    assert no_goals["closed"] is None and no_goals["stuck"] is None


def test_replay_mismatch_in_institution_audit_fails(tmp_path):
    evs = _events(tmp_path, [("run", "run_stopped", {"reason": "goal_met"})])
    check = {"I": {"I1": {"domain_events_without_allowed_act": 0, "replay": {
        "decision_mismatches": 0, "facts_equal": True, "ledger_equal": False}}}}
    assert run_metrics(evs, check, None, {})["replay_ok"] is False


def test_carry_exports_only_amended_files(tmp_path):
    rules, comm = "requirements: []\n", "rules: []\n"
    _events(tmp_path, [
        ("institution", "rule_version_activated", {
            "version": "v0", "files": {"r.yaml": sha256_hex("x"), "c.yaml": sha256_hex(comm)},
            "texts": {"r.yaml": "x", "c.yaml": comm}, "amendment": None}),
        ("amendment/A1", "amendment_enacted", {"amendment_id": "A1", "path": "r.yaml"}),
        ("institution", "rule_version_activated", {
            "version": "v1", "files": {"r.yaml": sha256_hex(rules), "c.yaml": sha256_hex(comm)},
            "texts": {"r.yaml": rules, "c.yaml": comm}, "amendment": "A1"})])
    out = carry(tmp_path, tmp_path / "ov")
    assert out == {"r.yaml": sha256_hex(rules)}
    assert (tmp_path / "ov" / "r.yaml").read_text() == rules
    assert not (tmp_path / "ov" / "c.yaml").exists()
    meta = json.loads((tmp_path / "ov" / "CARRIED.json").read_text())
    assert meta["rule_version"] == "v1" and meta["amendments"][0]["amendment_id"] == "A1"


def _result(out: Path, arm: str, task: str, r: int, **kw) -> None:
    d = out / arm / task / f"r{r}"
    d.mkdir(parents=True)
    base = {"arm": arm, "task": task, "r": r, "audit_ok": True, "graded": True,
            "replay_ok": True, "breaker": False, "closed": True, "stuck": False,
            "recovered": 0, "score": 0.5, "interpreter_digest": "D", "end_reason": "goal_met"}
    (d / RESULT).write_text(json.dumps({**base, **kw}))


def test_report_pairs_by_task_and_lists_exclusions(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "spec.yaml").write_text(yaml.safe_dump({
        "name": "e", "primary": "score", "theta": 0.5,
        "arms": [{"name": "a"}, {"name": "b"}]}))
    _result(out, "a", "x", 1, score=0.2)
    _result(out, "a", "y", 1, score=0.6)
    _result(out, "b", "x", 1, score=0.4)
    _result(out, "b", "y", 1, score=0.9)
    _result(out, "b", "y", 2, replay_ok=False)
    s = summarize(out)
    assert s["valid"]
    assert s["arms"]["a"]["false_closure"] == 0.5          # x closed below theta
    assert s["paired"]["b"]["mean_diff"] == pytest.approx(0.25)
    assert s["excluded"] == [{"arm": "b", "task": "y", "r": 2, "why": "replay mismatch"}]
    assert "VALID" in render_report(s)


def test_theta_per_task(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "spec.yaml").write_text(yaml.safe_dump({
        "name": "e", "primary": "score", "theta": {"x": 0.1, "y": 0.9},
        "arms": [{"name": "a"}]}))
    _result(out, "a", "x", 1, score=0.2)                    # above its theta
    _result(out, "a", "y", 1, score=0.6)                    # below its theta
    _result(out, "a", "z", 1, score=0.0)                    # no theta registered
    s = summarize(out)
    assert s["arms"]["a"]["false_closure"] == 0.5


def test_report_invalid_with_two_interpreters(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "spec.yaml").write_text(yaml.safe_dump({
        "name": "e", "primary": "score", "arms": [{"name": "a"}]}))
    _result(out, "a", "x", 1)
    _result(out, "a", "y", 1, interpreter_digest="E")
    s = summarize(out)
    assert not s["valid"] and "INVALID" in render_report(s)


def test_arm_model_defaults():
    assert Arm(name="a", team="t").overlay is None
    assert TaskSet(name="n", grader="g", tasks=[]).patches == {}
