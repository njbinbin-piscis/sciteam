"""Experiment runner (docs/60-experiment.md §3.2): arm × task × repeat → runs, each driven by
the existing team runtime with an automatic owner, then audited, graded and measured.

The runner adds no runtime semantics: every owner decision is an ordinary human Act."""

import asyncio
import hashlib
import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from sciteam.experiment.metrics import GOAL_REASONS, run_metrics
from sciteam.experiment.quota import (
    PAUSE_REASON,
    PAUSED_MARK,
    await_provider,
    is_paused,
    last_stop,
    models_for_spec,
    probe,
)
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
    for patches in ({**ts.patches, **task.patches}, arm.patches):
        for path, patch in patches.items():
            files[path] = patched_text(files.get(path) or cs.load_text(path),
                                       render_obj(patch, tv))
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
        reason = last_stop(run_dir)
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


IDENTITY = "identity.json"


def capture_identity(ts: TaskSet, tvars: dict[str, str], dest: Path) -> dict[str, str]:
    """Run the task set's identity commands once, when the run is created; a resumed run
    keeps what it started with. Each value is the sha256 of the command's output."""
    if dest.exists():
        return json.loads(dest.read_text())
    ident = {}
    for name, cmd in ts.identity.items():
        p = subprocess.run(render(cmd, tvars), shell=True, capture_output=True, timeout=1800)
        ident[name] = (hashlib.sha256(p.stdout).hexdigest() if p.returncode == 0
                       else f"error:{p.returncode}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(ident, indent=2))
    return ident


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
    tv = task_vars(ts, task)
    identity = capture_identity(ts, tv, rd / IDENTITY)
    if not (rd / "run.json").exists():
        opener = org_team.opener_for(configs_dir, arm.team, overlays)
        team.create_run(rd, team=arm.team, configs_dir=configs_dir, overlays=overlays,
                        opener=opener)
    elif is_paused(rd):
        # Account topped up: clear the provider breaker; the scheduler resumes the run (D13).
        team.reset(rd, breaker=True)
        (rd / PAUSED_MARK).unlink(missing_ok=True)
    owner = drive(rd, spec, tf)
    if is_paused(rd):
        # Halted by the provider account again: no result, so the supervisor pauses and retries.
        (rd / PAUSED_MARK).write_text(json.dumps(
            {"stop": PAUSE_REASON, "note": "resume with `sciteam exp run` after topping up"},
            indent=2))
        return {"arm": key.arm, "task": key.task, "r": key.r, "status": "paused"}
    log = rd / "experiment.log"
    check_out, score_out = rd / "check.json", rd / "score.json"
    q = shlex.quote
    _sh(spec.audit.format(run=q(str(rd)), out=q(str(check_out))), log)
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
              **run_metrics(evs, check, score, owner), "identity": identity}
    (rd / RESULT).write_text(json.dumps(result, indent=2))
    return result


def _supervisor_log(out: Path):
    path = out / "supervisor.log"

    def say(msg: str) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}"
        print(line, flush=True)
        with path.open("a") as f:
            f.write(line + "\n")
    return say


def _launch(configs_dir: Path, spec_path: str, out: Path, keys: list[RunKey], parallel: int) -> None:
    """Each run in its own process (one scheduler per process), `parallel` at a time."""
    def one(k: RunKey) -> None:
        rd = k.dir(out)
        rd.mkdir(parents=True, exist_ok=True)
        with (rd.parent / f"r{k.r}.driver.log").open("a") as f:
            subprocess.call([sys.executable, "-m", "sciteam.cli.main", "exp", "one", spec_path,
                             "--out", str(out), "--configs", str(configs_dir),
                             "--arm", k.arm, "--task", k.task, "--r", str(k.r)],
                            stdout=f, stderr=subprocess.STDOUT)

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        list(ex.map(one, keys))


def run_all(configs_dir: Path, spec_path: str, out: Path, *, poll_s: float = 600.0,
            max_pause_s: float = 48 * 3600.0, probe_fn=probe, log=None) -> list[dict]:
    """Supervise the experiment until every run has a result (docs/60 §11 D13).

    Before each launch every model the spec uses is probed; while the account is not ready the
    supervisor waits `poll_s` between probes, up to `max_pause_s` in total. A run halted by the
    provider account pauses and is resumed by the next launch. Stops when all runs have results,
    when the wait budget is spent, or after three launches without progress that needed no wait
    (the probe passed but runs keep failing). Re-running the command is safe: finished runs are
    skipped and paused runs resume."""
    spec, ts = load_spec(ConfigStore(configs_dir), spec_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "spec.yaml").write_text(ConfigStore(configs_dir).load_text(spec_path))
    keys = plan(spec, ts)
    models = models_for_spec(configs_dir, spec, ts)
    say = log or _supervisor_log(out)

    def open_keys() -> list[RunKey]:
        return [k for k in keys if not (k.dir(out) / RESULT).exists()]

    waited, stalled = 0.0, 0
    while open_keys():
        ready, w = await_provider(models, poll_s=poll_s, max_wait_s=max_pause_s - waited,
                                  probe_fn=probe_fn, log=say)
        waited += w
        if not ready:
            say(f"stopped: provider not ready after {waited / 3600:.1f} h; "
                f"{len(open_keys())} run(s) open; re-run to resume")
            break
        before = len(open_keys())
        say(f"launching {before} open run(s), parallel {spec.parallel}")
        _launch(configs_dir, spec_path, out, open_keys(), spec.parallel)
        after = len(open_keys())
        paused = [k for k in open_keys() if is_paused(k.dir(out))]
        if paused:
            say(f"{len(paused)} run(s) paused by the provider account")
        if after == before and w == 0:
            stalled += 1
            if stalled >= 3:
                say("stopped: three launches without progress although the probe passed")
                break
        else:
            stalled = 0
    return [json.loads((k.dir(out) / RESULT).read_text()) for k in keys
            if (k.dir(out) / RESULT).exists()]
