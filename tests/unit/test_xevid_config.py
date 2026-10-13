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
            assert roles == ["impl1", "impl2"]
            assert {t.assignee for t in tf.tasks} == {"impl1", "impl2"}
            from sciteam.org import team as org_team

            opener = org_team.opener_for(CONFIGS, arm.team, overlays)
            team.create_run(rd, team=arm.team, configs_dir=CONFIGS, overlays=overlays, opener=opener)
            tr = opener(rd)
            try:
                assert sorted(tr.comm.view.members) == ["impl1", "impl2", "owner"]
            finally:
                tr.close()


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
        store.append("task", "task_created", {"task": {"id": tid, "labels": ["goal"]}}, actor="owner")
    store.append("fact", "fact_established", {"subject": "T1", "fact": "accepted"}, actor="system")
    store.close()
    assert grade_run.closed(run) is False
    store = EventStore(run / "events.db")
    store.append("fact", "fact_established", {"subject": "T2", "fact": "accepted"}, actor="system")
    store.close()
    assert grade_run.closed(run) is True
