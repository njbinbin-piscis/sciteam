"""docs/60-experiment.md §6.6 v2: X-evid configuration wiring (no model calls, no docker)."""

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"
sys.path.insert(0, str(ROOT / "bench" / "xevid"))

from sciteam.experiment.runner import overlays_for, task_overlay  # noqa: E402
from sciteam.experiment.spec import TaskSet, load_spec  # noqa: E402
from sciteam.kernel.config_store import ConfigStore  # noqa: E402
from sciteam.kernel.events import EventStore  # noqa: E402
from sciteam.runtime import team  # noqa: E402
from sciteam.runtime.config import RuntimeConfig, TeamFile  # noqa: E402


def _spec():
    return load_spec(ConfigStore(CONFIGS), "experiments/x-evid.yaml")


def _arm(spec, name):
    return next(a for a in spec.arms if a.name == name)


def test_spec_has_three_arms_and_the_confirm_problems():
    spec, ts = _spec()
    assert [a.name for a in spec.arms] == ["replicate-same", "replicate-cross", "compute-control"]
    assert len(spec.only_tasks) == 24
    assert set(spec.only_tasks) <= {t.id for t in ts.tasks}


def test_taskset_is_the_derived_one_and_grades_private_solutions():
    raw = yaml.safe_load((CONFIGS / "tasksets/xevid-scicode.yaml").read_text())
    ts = TaskSet.model_validate(raw)
    assert "grade_run.py" in ts.grader and "{workspace}" in ts.grader
    assert "runtime/default.yaml" not in ts.patches
    assert "harness/team-xevid-b.yaml" in ts.patches


def test_every_task_has_a_prebuilt_bundle_of_inputs_only():
    _, ts = _spec()
    for t in ts.tasks:
        path = ROOT / "bench" / "xevid" / "bundles" / t.id.lstrip("p") / "bundle.json"
        assert path.exists(), path
        assert "targets[" not in path.read_text()


def test_agreement_arms_mount_the_validator_and_the_control_does_not():
    spec, _ = _spec()
    for name in ("replicate-same", "replicate-cross"):
        mounts = _arm(spec, name).patches["runtime/x-evid.yaml"]["sandbox"]["validator_mounts"]
        assert any(m.startswith("bench/xevid/agree.py:/xevid/agree.py") for m in mounts)
        assert any(m.startswith("bench/xevid/bundles/{task.pid}:/xevid/bundle") for m in mounts)
    assert "runtime/x-evid.yaml" not in _arm(spec, "compute-control").patches


def test_cross_arm_gives_the_second_implementer_another_model():
    spec, _ = _spec()
    assert _arm(spec, "replicate-cross").patches["harness/team-xevid-b.yaml"] == {"model": "mimo-v2.6-flash"}
    assert "harness/team-xevid-b.yaml" not in _arm(spec, "replicate-same").patches


def test_each_arm_builds_a_team_with_two_implementers_and_a_goal_per_implementer():
    spec, ts = _spec()
    task = next(t for t in ts.tasks if t.id == "p13")
    for arm in spec.arms:
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / arm.name / "r1"
            tdir = task_overlay(CONFIGS, arm, ts, task, rd.parent / "overlay-r1")
            overlays = overlays_for(CONFIGS, arm, tdir)
            tf = ConfigStore(CONFIGS, overlays).load(arm.team, TeamFile)
            roles = sorted(m.id for m in tf.members if m.kind == "agent")
            assert roles == ["auditor", "impl1", "impl2"]
            assert {t.assignee for t in tf.tasks} == {"impl1", "impl2"}
            assert {t.reviewer for t in tf.tasks} == {"auditor"}
            from sciteam.org import team as org_team

            opener = org_team.opener_for(CONFIGS, arm.team, overlays)
            team.create_run(rd, team=arm.team, configs_dir=CONFIGS, overlays=overlays, opener=opener)
            tr = opener(rd)
            try:
                assert sorted(tr.comm.view.members) == ["auditor", "impl1", "impl2", "owner"]
            finally:
                tr.close()


def test_quota_probe_covers_the_cross_arm_model_override():
    from sciteam.experiment.quota import models_for_spec

    spec, ts = _spec()
    names = {m.name for m in models_for_spec(CONFIGS, spec, ts)}
    assert {"deepseek-flash", "mimo-v2.6-flash"} <= names


def test_runtime_is_docker_with_private_mounts_and_the_sci_image():
    rt = RuntimeConfig.model_validate(yaml.safe_load((CONFIGS / "runtime/x-evid.yaml").read_text()))
    assert rt.sandbox.kind == "docker"
    assert rt.sandbox.private_mount == "/private"
    assert rt.sandbox.image == "sciteam-sci:v0"


def test_goal_closes_only_when_both_goal_tasks_are_accepted(tmp_path):
    import grade_run

    run = tmp_path / "run"
    run.mkdir()
    store = EventStore(run / "events.db")
    for tid in ("T1", "T2"):
        store.append(
            "task",
            "task_created",
            {"task": {"id": tid, "labels": ["goal"], "created_by": "owner"}},
            actor="owner",
        )
    store.append("fact", "fact_established", {"subject": "T1", "fact": "accepted"}, actor="system")
    store.close()
    assert grade_run.closed(run) is False
    store = EventStore(run / "events.db")
    store.append("fact", "fact_established", {"subject": "T2", "fact": "accepted"}, actor="system")
    store.close()
    assert grade_run.closed(run) is True


def test_validator_commands_are_fixed_in_the_rules_and_scoped_to_goal_tasks():
    # a member-authored acceptance command ran in a sandbox that sees every private directory
    # and read the peer's file (2026-10-17 smoke), so no validator may take the task's field
    for name in ("x-evid-rules", "x-evid-control-rules"):
        rules = yaml.safe_load((CONFIGS / f"institution/{name}.yaml").read_text())
        for v in rules["validators"].values():
            assert "{" not in v["command"]
        assert all(r.get("where") == {"task_label": "goal"} for r in rules["requirements"])
        assert not any(c.get("field") == "acceptance" for r in rules["requirements"] for c in r["require"])
    agree = yaml.safe_load((CONFIGS / "institution/x-evid-rules.yaml").read_text())
    assert "/xevid/agree.py" in agree["validators"]["agree"]["command"]
    assert any(
        r["transition"] == "approve" and {"validate": ["agree"]} in r["require"]
        for r in agree["requirements"]
    )



def test_goal_tasks_created_by_a_member_do_not_count(tmp_path):
    import grade_run

    run = tmp_path / "run"
    run.mkdir()
    store = EventStore(run / "events.db")
    for tid, by in (("T1", "owner"), ("T2", "owner"), ("T9", "impl1")):
        store.append(
            "task", "task_created", {"task": {"id": tid, "labels": ["goal"], "created_by": by}}, actor=by
        )
    for tid in ("T1", "T2"):
        store.append("fact", "fact_established", {"subject": tid, "fact": "accepted"}, actor="system")
    store.close()
    assert grade_run.closed(run) is True


def test_members_cannot_hold_the_run_open_with_timers_while_waiting_for_the_owner():
    # the automatic owner acts only between scheduler runs; a member re-arming wake-up timers
    # while its task waits in review keeps the run from ever going quiescent (smoke 2026-10-17)
    rt = RuntimeConfig.model_validate(
        yaml.safe_load((CONFIGS / "runtime/x-evid.yaml").read_text()))
    assert rt.rate.max_timers_per_agent == 0


def test_double_blind_comm_policy_prevents_implementers_from_messaging_each_other():
    # D18 (smoke 2026-10-17): peer DMs during reconciliation collapsed statistical independence
    from sciteam.comm.model import Act
    from sciteam.org import team as org_team

    spec, ts = _spec()
    task = next(t for t in ts.tasks if t.id == "p13")
    for arm in spec.arms:
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / arm.name / "r1"
            tdir = task_overlay(CONFIGS, arm, ts, task, rd.parent / "overlay-r1")
            overlays = overlays_for(CONFIGS, arm, tdir)
            opener = org_team.opener_for(CONFIGS, arm.team, overlays)
            team.create_run(rd, team=arm.team, configs_dir=CONFIGS, overlays=overlays, opener=opener)
            tr = opener(rd)
            try:
                assert "room:general" not in tr.comm.view.convs
                dm = tr.comm.submit(Act(act_id="a1", actor="impl1", kind="post_message",
                                        params={"to": "@impl2", "text": "share formula"}))
                assert not dm.ok
                room = tr.comm.submit(Act(act_id="a2", actor="impl1", kind="create_room",
                                          params={"name": "sync", "members": ["impl2"]}))
                assert not room.ok
                cross_task = tr.comm.submit(Act(act_id="a3", actor="impl1", kind="post_message",
                                                params={"to": "T2", "text": "my output is 1.23"}))
                assert not cross_task.ok
                own_task = tr.comm.submit(Act(act_id="a4", actor="impl1", kind="post_message",
                                              params={"to": "T1", "text": "working on step 1"}))
                assert own_task.ok
                to_owner = tr.comm.submit(Act(act_id="a5", actor="impl1", kind="post_message",
                                              params={"to": "@owner", "text": "status update"}))
                assert to_owner.ok
                to_auditor = tr.comm.submit(Act(act_id="a6", actor="impl1", kind="post_message",
                                                params={"to": "@auditor", "text": "hint"}))
                assert not to_auditor.ok
            finally:
                tr.close()


def test_agree_shim_supports_scicode_cmp_and_tuple_comparison():
    import agree

    bundle = {
        "problem_id": "shim_test",
        "head": "import os, pathlib\nassert os.environ.get('HOME') and os.environ.get('MPLCONFIGDIR')",
        "tail": "",
        "cases": [
            {
                "sid": "1.1",
                "i": 0,
                "setup": "from scicode.compare.cmp import cmp_tuple_or_list\nx = 2.0",
                "expr": "f(x)",
                "cmp": "cmp_tuple_or_list(_a, target)",
            }
        ],
        "excluded": 0,
    }
    rep_ok = agree.check(bundle, "def f(x):\n    return ([x, x + 1.0], True)\n",
                         "def f(x):\n    return ([2.0, 3.0], True)\n")
    assert rep_ok["agree"] is True
    assert rep_ok["failed"] == []
    rep_bad = agree.check(bundle, "def f(x):\n    return ([x, x + 1.0], True)\n",
                          "def f(x):\n    return ([2.0, 4.0], True)\n")
    assert rep_bad["agree"] is False
    assert rep_bad["failed"] == [{"sid": "1.1", "i": 0, "reason": "mismatch"}]


