"""Experiment runner (docs/60-experiment.md §3.2): arm × task × repeat → runs, each driven by
the existing team runtime with an automatic owner, then audited, graded and measured.

The runner adds no runtime semantics: every owner decision is an ordinary human Act."""

import asyncio
import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

from sciteam.experiment.metrics import GOAL_REASONS, run_metrics
from sciteam.experiment.spec import (
    Arm,
    ExperimentSpec,
    TaskSet,
    TaskSpec,
    dump,
    load_spec,
    patched_text,
    render,
    render_obj,
    task_vars,
)
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import EventStore
from sciteam.runtime.config import TeamFile

RESULT = "result.json"


@dataclass(frozen=True)
class RunKey:
    arm: str
    task: str
    r: int

    def dir(self, out: Path) -> Path:
        return out / self.arm / self.task / f"r{self.r}"


def plan(spec: ExperimentSpec, ts: TaskSet) -> list[RunKey]:
    """Repeat-major, then task, then arm: arms of one repeat finish close together in time."""
    return [RunKey(a.name, t.id, r) for r in range(1, spec.repeats + 1)
            for t in ts.tasks for a in spec.arms]


def task_overlay(configs_dir: Path, arm: Arm, ts: TaskSet, task: TaskSpec, dest: Path) -> Path:
    """Write the per-(arm, task) overlay: the arm's team file with {task.*} filled in, and
    the task set's patches merged into the files as the arm sees them."""
    tv = task_vars(ts, task)
    base = [str(configs_dir / arm.overlay)] if arm.overlay else []
    cs = ConfigStore(configs_dir, base)
    files = {arm.team: dump(render_obj(yaml.safe_load(cs.load_text(arm.team)), tv))}
    for path, patch in {**ts.patches, **task.patches}.items():
        files[path] = patched_text(files.get(path) or cs.load_text(path), render_obj(patch, tv))
    for path, text in files.items():
        (dest / path).parent.mkdir(parents=True, exist_ok=True)
        (dest / path).write_text(text)
    return dest


def overlays_for(configs_dir: Path, arm: Arm, task_dir: Path) -> list[Path]:
    return ([configs_dir / arm.overlay] if arm.overlay else []) + [task_dir]


def _owner_and_target(tf: TeamFile) -> tuple[str, str]:
    humans = [m.id for m in tf.members if m.kind == "human"]
    if not humans:
        raise ValueError(f"team {tf.name} has no human member to act as owner")
    k = next((k for k in tf.kickoff if k.by == humans[0]), None)
    return humans[0], (k.to if k else "")


def _awaiting_owner(run_dir: Path, owner: str) -> tuple[list[str], list[str]]:
    """(approvals the owner may decide, tasks in review with the owner as reviewer)."""
    from sciteam.institution.gate import act_task
    from sciteam.institution.team import InstitutionTeamRun
    from sciteam.org.team import open_run

    tr = open_run(run_dir)
    try:
        reviews = sorted(t.id for t in tr.comm.view.tasks.values()
                         if t.status == "in_review" and t.reviewer == owner)
        if not isinstance(tr, InstitutionTeamRun):
            return [], reviews
        svc = tr.institution
        svc.settle()
        keys = []
        for st in svc.inst.approvals.values():
            if st.status != "requested":
                continue
            subject = svc.inst.acts.get(st.key)
            task = act_task(subject, svc.comm.view) if subject else None
            if svc.selector_holds(st.approver, owner, owner, task):
                keys.append(st.key)
        return keys, reviews
    finally:
        tr.close()


def _last_stop(run_dir: Path) -> str | None:
    store = EventStore(run_dir / "events.db")
    try:
        stops = [e.payload.get("reason") for e in store.read_global(types={"run_stopped"})]
    finally:
        store.close()
    return stops[-1] if stops else None


def drive(run_dir: Path, spec: ExperimentSpec, tf: TeamFile) -> dict[str, int]:
    """Run the team to its end, acting as the owner between scheduler runs."""
    from sciteam.institution.team import decide
    from sciteam.org.team import open_run
    from sciteam.runtime.team import post, run_team

    owner, target = _owner_and_target(tf)
    tally = {"decisions": 0, "reviews": 0, "nudges": 0, "rounds": 0}
    for _ in range(spec.owner.max_rounds):
        tally["rounds"] += 1
        asyncio.run(run_team(run_dir, opener=open_run))
        reason = _last_stop(run_dir)
        if reason in GOAL_REASONS or (reason or "").startswith("breaker"):
            break
        keys, reviews = _awaiting_owner(run_dir, owner) \
            if spec.owner.mode == "auto-approve" else ([], [])
        if keys or reviews:
            for key in keys:
                decide(run_dir, as_=owner, id=key, decision="approve",
                       note="Approved (automatic owner).", opener=open_run)
                tally["decisions"] += 1
            for tid in reviews:
                act(run_dir, as_=owner, kind="task_transition",
                    params={"task": tid, "transition": "approve",
                            "note": "Approved (automatic owner)."})
                tally["reviews"] += 1
            continue
        if tally["nudges"] < spec.owner.nudges and target:
            post(run_dir, as_=owner, to=target, text=spec.owner.nudge_text, opener=open_run)
            tally["nudges"] += 1
            continue
        break
    return tally


def act(run_dir: Path, *, as_: str, kind: str, params: dict) -> bool:
    """A human Act from outside the scheduler, recorded like any other (then settled)."""
    from sciteam.comm.model import Act
    from sciteam.institution.team import InstitutionTeamRun
    from sciteam.kernel.ids import new_id
    from sciteam.org.team import open_run

    tr = open_run(run_dir)
    try:
        r = tr.comm.submit(Act(act_id=new_id("act"), actor=as_, kind=kind, params=params))
        if isinstance(tr, InstitutionTeamRun):
            tr.settle()
        return r.ok
    finally:
        tr.close()


def _sh(cmd: str, log: Path) -> int:
    with log.open("a") as f:
        f.write(f"$ {cmd}\n")
        f.flush()
        return subprocess.call(cmd, shell=True, stdout=f, stderr=subprocess.STDOUT,
                               cwd=Path(__file__).resolve().parents[2])


def run_one(configs_dir: Path, spec_path: str, out: Path, key: RunKey) -> dict:
    from sciteam.org import team as org_team
    from sciteam.runtime import team

    configs_dir = configs_dir.resolve()
    spec, ts = load_spec(ConfigStore(configs_dir), spec_path)
    arm = next(a for a in spec.arms if a.name == key.arm)
    task = next(t for t in ts.tasks if t.id == key.task)
    rd = key.dir(out).resolve()
    if (rd / RESULT).exists():
        return json.loads((rd / RESULT).read_text())
    tdir = task_overlay(configs_dir, arm, ts, task, rd.parent / f"overlay-r{key.r}")
    overlays = overlays_for(configs_dir, arm, tdir)
    tf = ConfigStore(configs_dir, overlays).load(arm.team, TeamFile)
    if not (rd / "run.json").exists():
        opener = org_team.opener_for(configs_dir, arm.team, overlays)
        team.create_run(rd, team=arm.team, configs_dir=configs_dir, overlays=overlays,
                        opener=opener)
    owner = drive(rd, spec, tf)
    log = rd / "experiment.log"
    check_out, score_out = rd / "check.json", rd / "score.json"
    q = shlex.quote
    _sh(spec.audit.format(run=q(str(rd)), out=q(str(check_out))), log)
    tv = task_vars(ts, task)
    _sh(render(ts.grader, tv).format(workspace=q(str(rd / "workspace")), out=q(str(score_out))),
        log)
    check = json.loads(check_out.read_text()) if check_out.exists() else {}
    score = json.loads(score_out.read_text()) if score_out.exists() else None
    store = EventStore(rd / "events.db")
    try:
        evs = store.read_global()
    finally:
        store.close()
    result = {"arm": key.arm, "task": key.task, "r": key.r,
              "audit_ok": bool(check), "graded": score is not None,
              **run_metrics(evs, check, score, owner)}
    (rd / RESULT).write_text(json.dumps(result, indent=2))
    return result


def run_all(configs_dir: Path, spec_path: str, out: Path) -> list[dict]:
    """Each run in its own process (one scheduler per process), `parallel` at a time."""
    spec, ts = load_spec(ConfigStore(configs_dir), spec_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "spec.yaml").write_text(ConfigStore(configs_dir).load_text(spec_path))
    keys = plan(spec, ts)

    def one(k: RunKey) -> dict | None:
        rd = k.dir(out)
        if (rd / RESULT).exists():
            return json.loads((rd / RESULT).read_text())
        rd.mkdir(parents=True, exist_ok=True)
        with (rd.parent / f"r{k.r}.driver.log").open("a") as f:
            subprocess.call([sys.executable, "-m", "sciteam.cli.main", "exp", "one", spec_path,
                             "--out", str(out), "--configs", str(configs_dir),
                             "--arm", k.arm, "--task", k.task, "--r", str(k.r)],
                            stdout=f, stderr=subprocess.STDOUT)
        return json.loads((rd / RESULT).read_text()) if (rd / RESULT).exists() else None

    with ThreadPoolExecutor(max_workers=spec.parallel) as ex:
        return [r for r in ex.map(one, keys) if r is not None]
