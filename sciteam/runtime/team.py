"""Team runs: composition root, creation, resume, and the human interface (31 §1, §8, §9)."""

import fcntl
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sciteam.comm import Act, CommPolicy, CommService, Member
from sciteam.comm.render import render_board
from sciteam.harness import HarnessConfig
from sciteam.harness.memory import MemoryBook, render_memory
from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.clock import Clock, SystemClock
from sciteam.kernel.config_store import ConfigStore, split_front_matter
from sciteam.kernel.events import EventStore
from sciteam.kernel.ids import new_id
from sciteam.providers.llm import load_models, make_provider
from sciteam.providers.llm.types import ModelInfo
from sciteam.runtime.agent import AgentRunner, LlmFactory
from sciteam.runtime.attention import still_relevant
from sciteam.runtime.config import OfflineConfig, RuntimeConfig, TeamFile, benchmark_problems
from sciteam.runtime.scheduler import RUN, Scheduler, SchedulerExtension

RUN_FILE = "run.json"
FROZEN_CONFIGS = "configs"
MEMORY_DIR = "memory"


def _default_llm_factory(agent: str, info) -> Any:
    return make_provider(info)


class MemoryBriefing:
    """Every activation starts with the notes that apply to its member."""

    def __init__(self, book: MemoryBook, comm: CommService):
        self.book, self.comm = book, comm

    def attention(self, ev, view) -> list:
        return []

    def busy(self) -> bool:
        return False

    def step(self) -> None:
        pass

    def briefing(self, member: str) -> list[tuple[str, str]]:
        m = self.comm.view.members.get(member)
        role = m.role if m else ""
        notes = [n for n in self.book.notes()
                 if n.applies_to(member, role) or n.scope.startswith("tool:")]
        return [("Memory: notes", render_memory(notes, self.book.cfg))]

    async def close(self) -> None:
        pass


class TeamRun:
    """Everything one team run needs, built from run.json + configs. Higher layers subclass it
    and override the hooks (_make_comm, on_create, extensions, extra_tools, tool_extras)."""

    def __init__(self, run_dir: Path, *, llm_factory: LlmFactory | None = None,
                 clock: Clock | None = None, models: dict[str, ModelInfo] | None = None):
        self.run_dir = run_dir
        meta = json.loads((run_dir / RUN_FILE).read_text())
        self.meta = meta
        self.clock = clock or SystemClock()
        self.configs = ConfigStore(meta["configs_dir"], meta.get("overlays", []))
        self.team = self.configs.load(meta["team"], TeamFile)
        self.policy = self.configs.load(self.team.comm_policy, CommPolicy)
        self.runtime = self.configs.load(self.team.runtime, RuntimeConfig)
        self.store = EventStore(run_dir / "events.db", clock=self.clock)
        self.blobs = BlobStore(run_dir / "blobs")
        self.workspace = run_dir / "workspace"
        self.memory = MemoryBook(run_dir / MEMORY_DIR, self.configs.base / MEMORY_DIR,
                                 self.runtime.memory)
        self.comm = self._make_comm()
        self.llm_factory = llm_factory or _default_llm_factory
        self.models = models

    # ---- hooks --------------------------------------------------------------------------
    def _make_comm(self) -> CommService:
        return CommService(self.store, self.policy, self.clock)

    def on_create(self) -> None:
        """After the roster and rooms exist, before the first Act of the run."""

    def extensions(self, sched: Scheduler) -> list[SchedulerExtension]:
        return []

    def extra_tools(self) -> dict[str, Any]:
        return {}

    def tool_extras(self) -> dict[str, Any]:
        return {}

    def status_lines(self) -> list[str]:
        return []

    @property
    def goal(self) -> str:
        return self.meta.get("goal", "")

    def scheduler(self) -> Scheduler:
        models = self.models if self.models is not None else \
            load_models(Path(self.meta["configs_dir"]) / "models.yaml")
        sched: Scheduler | None = None

        def active_timers(agent: str) -> int:
            return sum(1 for a, _, _ in sched.timers.values() if a == agent) if sched else 0

        runner = AgentRunner(store=self.store, blobs=self.blobs, comm=self.comm,
                             configs=self.configs, models=models,
                             llm_factory=self.llm_factory, runtime=self.runtime,
                             workspace=self.workspace, clock=self.clock,
                             active_timers=active_timers, run_name=self.run_dir.name,
                             extra_tools=self.extra_tools(), tool_extras=self.tool_extras(),
                             memory=self.memory)
        sched = Scheduler(store=self.store, policy=self.comm.policy, runtime=self.runtime,
                          runner=runner, clock=self.clock, goal=self.goal)
        from sciteam.runtime.incoming import IncomingBriefing

        sched.extensions.append(MemoryBriefing(self.memory, self.comm))
        sched.extensions.append(IncomingBriefing(self.workspace))
        sched.extensions.extend(self.extensions(sched))
        return sched

    def close(self) -> None:
        self.store.close()


Opener = Callable[..., TeamRun]


def create_run(run_dir: Path, *, team: str, configs_dir: Path, goal: str | None = None,
               clock: Clock | None = None, overlays: list[Path] = (),
               opener: Opener = TeamRun, meta: dict[str, Any] | None = None) -> None:
    """Write run.json, seed the workspace and the genesis events (roster, rooms, tasks). The
    configs and overlays are copied into the run, which reads only that copy from then on."""
    if (run_dir / RUN_FILE).exists():
        raise FileExistsError(f"{run_dir} already holds a run; use resume")
    run_dir.mkdir(parents=True, exist_ok=True)
    source = {"configs_dir": str(Path(configs_dir).resolve()),
              "overlays": [str(Path(o).resolve()) for o in overlays],
              "commit": _git_head(Path(configs_dir))}
    configs_dir = freeze_configs(Path(configs_dir), [Path(o) for o in overlays],
                                 run_dir / FROZEN_CONFIGS)
    configs = ConfigStore(configs_dir)
    tf = configs.load(team, TeamFile)
    if goal is None:
        goal = configs.load_text(tf.goal_ref) if tf.goal_ref else ""
    (run_dir / RUN_FILE).write_text(json.dumps(
        {"team": team, "configs_dir": str(configs_dir.resolve()), "overlays": [],
         "goal": goal, "source": source, **(meta or {})}, indent=2))
    ws = run_dir / "workspace"
    if tf.workspace_seed:
        shutil.copytree(Path(configs_dir) / tf.workspace_seed, ws, dirs_exist_ok=True)
    ws.mkdir(exist_ok=True)
    incoming = ws / "data" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    readme = incoming / "README.txt"
    if not readme.exists():
        from sciteam.runtime.incoming import README_TEXT
        readme.write_text(README_TEXT, encoding="utf-8")
    _git_init(ws)

    tr = opener(run_dir, clock=clock)
    try:
        policy, comm = tr.policy, tr.comm
        members = []
        for tm in tf.members:
            profile = split_front_matter(configs.load_text(tm.profile_ref))[1] \
                .replace("{member_id}", tm.id) if tm.profile_ref else ""
            m = Member(id=tm.id, kind=tm.kind, role=tm.role, team=tm.team, manager=tm.manager,
                       harness=tm.harness, profile=profile, attrs=tm.attrs)
            members.append(m)
        for m in members:
            comm.register_member(m)
        for spec in policy.rooms:
            _policy_room(comm, spec)
        for r in tf.rooms:
            comm.system_room(r.name, set(r.members), r.topic)
        tr.on_create()
        keys: dict[str, str] = {}
        for pt in tf.tasks:
            deps = []
            for d in pt.deps:
                d = {"task": d} if isinstance(d, str) else dict(d)
                d["task"] = keys[d["task"]]
                deps.append(d)
            params = {"title": pt.title, "description": pt.description, "deps": deps,
                      "assignee": pt.assignee, "reviewer": pt.reviewer,
                      "estimate_h": pt.estimate_h, "labels": pt.labels or None,
                      "acceptance": pt.acceptance,
                      "parent": keys[pt.parent] if pt.parent else None}
            res = comm.submit(Act(act_id=new_id("act"), actor=pt.by, kind="task_create",
                                  params={k: v for k, v in params.items() if v is not None}))
            if not res.ok:
                raise ValueError(f"preset task '{pt.key}' rejected: {res.decision.reason}")
            keys[pt.key] = res.data["task"]
        for k in tf.kickoff:
            res = comm.submit(Act(act_id=new_id("act"), actor=k.by, kind="post_message",
                                  params={"to": k.to, "text": k.text}))
            if not res.ok:
                raise ValueError(f"kickoff message rejected: {res.decision.reason}")
        for m in members:
            if m.harness:  # record every harness config and prompt in the snapshot
                configs.load_text(configs.load(m.harness, HarnessConfig).system_prompt_ref)
        configs.load(tf.comm_policy, CommPolicy)
        if tf.institution:
            configs.load_text(tf.institution)
        if tf.org:
            configs.load_text(tf.org)
        rt = configs.load(tf.runtime, RuntimeConfig)
        bad = benchmark_problems(rt)
        if bad:
            raise ValueError("benchmark run refused: " + "; ".join(bad))
        snap = configs.snapshot()
        tr.store.append(RUN, "run_started", {"team": team, "goal": goal, "mode": rt.mode,
                                             "image": rt.sandbox.image,
                                             "config": snap.model_dump(mode="json")},
                        actor="system")
    finally:
        tr.close()


def freeze_configs(base: Path, overlays: list[Path], dest: Path) -> Path:
    """The base configs with every overlay layered over them, as one directory."""
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
    shutil.copytree(base, dest, ignore=ignore, dirs_exist_ok=True)
    for o in overlays:
        if Path(o).is_dir():
            shutil.copytree(o, dest, ignore=ignore, dirs_exist_ok=True)
    return dest


def _git_head(path: Path) -> str | None:
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                         text=True)
    return out.stdout.strip() or None if out.returncode == 0 else None


def _policy_room(comm: CommService, spec) -> None:
    from sciteam.comm.policy import SelCtx, compile_selector

    view = comm.view
    teams = sorted({m.team for m in view.members.values() if m.team}) \
        if spec.for_each == "team" else [None]
    for team in teams:
        name = spec.name.replace("{team}", team or "")
        sel = compile_selector(spec.members.replace("{team}", team or ""))
        ctx = SelCtx(view=view, actor="system")
        members = {m for m in view.members if sel(m, ctx)}
        if members:
            comm.system_room(name, members, spec.topic)


def _git_init(ws: Path) -> None:
    if (ws / ".git").exists():
        return
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=team@sciteam", "-c", "user.name=sciteam", "add",
                    "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=team@sciteam", "-c", "user.name=sciteam", "commit",
                    "-q", "--allow-empty", "-m", "seed"], cwd=ws, check=True)


class RunLocked(RuntimeError):
    pass


async def run_team(run_dir: Path, opener: Opener = TeamRun, **kw) -> str:
    # Exactly one scheduler per run; post/stop/status from other processes only append or read.
    with open(run_dir / "scheduler.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLocked(f"{run_dir} is already driven by another process") from None
        tr = opener(run_dir, **kw)
        try:
            return await tr.scheduler().run()
        finally:
            tr.close()


def post(run_dir: Path, *, as_: str, to: str, text: str, opener: Opener = TeamRun) -> str:
    tr = opener(run_dir)
    try:
        r = tr.comm.submit(Act(act_id=new_id("act"), actor=as_, kind="post_message",
                               params={"to": to, "text": text}))
        if r.ok and getattr(tr.runtime, "mode", "") == "benchmark":
            tr.store.append(RUN, "run_interjected", {"actor": as_, "to": to}, actor="human")
        return f"sent {r.data['msg_id']}" if r.ok else f"denied: {r.decision.reason}"
    finally:
        tr.close()


def request_stop(run_dir: Path) -> None:
    tr = TeamRun(run_dir)
    try:
        tr.store.append(RUN, "run_stop_requested", {}, actor="human")
    finally:
        tr.close()


def reset(run_dir: Path, *, breaker: bool = False, agent: str | None = None) -> None:
    tr = TeamRun(run_dir)
    try:
        if breaker:
            tr.store.append(RUN, "breaker_reset", {}, actor="human")
        if agent:
            tr.store.append(RUN, "agent_reset", {"agent": agent}, actor="human")
    finally:
        tr.close()


def decide_budget(run_dir: Path, pause_id: str, decision: str, *, as_: str = "owner",
                  add: dict[str, float] | None = None) -> None:
    """The owner's answer to a budget approval request: grant (optionally with explicit amounts
    per dimension, e.g. {"tokens": 5e6}) or decline. Read by the scheduler like any event."""
    if decision not in ("grant", "decline"):
        raise ValueError("decision must be grant or decline")
    tr = TeamRun(run_dir)
    try:
        tr.store.append(RUN, "budget_decided", {"pause_id": pause_id, "decision": decision,
                                                "actor": as_, "add": add or {}}, actor=as_)
    finally:
        tr.close()


def set_budget(run_dir: Path, *, as_: str = "owner", **settings: Any) -> None:
    """Change the run budget while it runs: tokens, wall_clock_s, auto_continue,
    auto_continue_max."""
    allowed = {"tokens", "wall_clock_s", "cost_usd", "auto_continue", "auto_continue_max"}
    bad = set(settings) - allowed
    if bad:
        raise ValueError(f"unknown budget settings {sorted(bad)}")
    tr = TeamRun(run_dir)
    try:
        tr.store.append(RUN, "budget_settings_changed", settings, actor=as_)
    finally:
        tr.close()


def set_offline(run_dir: Path, *, as_: str = "owner", **settings: Any) -> None:
    """Change what the team does while a decision waits for the owner."""
    OfflineConfig.model_validate(settings)
    bad = set(settings) - set(OfflineConfig.model_fields)
    if bad:
        raise ValueError(f"unknown offline settings {sorted(bad)}")
    tr = TeamRun(run_dir)
    try:
        tr.store.append(RUN, "offline_settings_changed", settings, actor=as_)
    finally:
        tr.close()


def budget_view(store: EventStore) -> dict[str, Any]:
    """Tokens used, run budget settings in force, and approval requests still open."""
    requests: dict[str, dict] = {}
    decided: set[str] = set()
    settings: dict[str, Any] = {}
    run_granted: dict[str, float] = {}
    tokens = auto_grants = 0
    for ev in store.read_global():
        p = ev.payload
        if ev.type in ("model_returned", "summary_usage") and ev.stream.startswith("agent/"):
            u = p.get("usage") or {}
            tokens += int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
        elif ev.type == "budget_approval_requested":
            requests[p["pause_id"]] = {**p, "ts": ev.ts.isoformat()}
        elif ev.type == "budget_decided":
            decided.add(p["pause_id"])
            auto_grants += p.get("actor") == "auto_policy"
            req = requests.get(p["pause_id"]) or {}
            if req.get("scope") == "run" and p.get("decision") == "grant":
                for dim, amount in (p.get("add") or req.get("default_add") or {}).items():
                    run_granted[dim] = run_granted.get(dim, 0.0) + float(amount)
        elif ev.type == "budget_settings_changed":
            settings.update(p)
    return {"tokens_used": tokens, "settings": settings, "run_granted": run_granted,
            "auto_grants": auto_grants,
            "pending": [r for pid, r in requests.items() if pid not in decided]}


def status(run_dir: Path, opener: Opener = TeamRun) -> str:
    tr = opener(run_dir)
    try:
        s = tr.scheduler()
        s.catch_up()
        lines = [f"run {run_dir.name}: tokens {s.tokens_used / 1e6:.3f}M; paused={s.paused}; "
                 f"stopped={s.stopped}"]
        open_requests = [r for pid, r in s.requests.items() if pid not in s.decisions]
        for r in open_requests:
            lines.append(f"- budget approval {r['pause_id']} ({r['scope']}"
                         f"{', @' + r['agent'] if r.get('agent') else ''}): waiting for owner")
        for name, st in sorted(s.agents.items()):
            if name not in s.view.members:
                continue
            state = "running" if st.running else "faulted" if st.faulted else "idle"
            live = [i for i in st.pending if still_relevant(i, s.view)]
            waking = sum(1 for i in live if i.wakes)
            lines.append(f"- @{name}: {state}; activations {len(st.starts)}; pending "
                         f"{len(live)} ({waking} waking)")
        for h, items in s.human_inbox.items():
            lines.append(f"- inbox @{h}: {len(items)} item(s)")
        lines += tr.status_lines()
        lines += ["", render_board(s.view, include_closed=True)]
        return "\n".join(lines)
    finally:
        tr.close()
