"""Event-driven scheduler: no rounds; whoever has attention gets an activation
(docs/31-runtime.md §3–§6). All scheduler state is a fold of the event store."""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel

from sciteam.comm.model import TERMINAL
from sciteam.comm.policy import CommPolicy, PolicyGate
from sciteam.comm.view import CommView
from sciteam.kernel.clock import Clock
from sciteam.kernel.events import Event, EventStore
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.types import HALT_REASONS
from sciteam.runtime.agent import AgentRunner, activation_stream
from sciteam.runtime.attention import (
    AttentionItem,
    agent_of_activation_stream,
    attention_for,
    still_relevant,
)
from sciteam.runtime.briefing import MemoryEntry, render_briefing, render_incoming
from sciteam.runtime.config import RuntimeConfig

RUN = "run"
MAX_WAIT_S = 1.0


class AgentState(BaseModel):
    pending: list[AttentionItem] = []
    delivered: set[int] = set()
    running: str | None = None           # activation id
    faulted: bool = False
    errors: int = 0                      # consecutive error outcomes
    handoffs: int = 0                    # consecutive handoff/budget outcomes
    memory: list[MemoryEntry] = []
    starts: list[datetime] = []
    last_finish: datetime | None = None


class SchedulerExtension(Protocol):
    """What a higher layer adds to the scheduler without changing its semantics."""

    def attention(self, ev: Event, view: CommView) -> list[AttentionItem]: ...

    def busy(self) -> bool:
        """True while work outside activations is under way; the run is then not quiescent."""
        ...

    def step(self) -> None:
        """Called on every scheduler iteration, before activations are started."""
        ...

    # Optional: `stop_reason() -> str | None` — the run's end condition holds (e.g. goal_met).

    def briefing(self, member: str) -> list[tuple[str, str]]:
        """Extra (heading, body) sections of that member's activation briefing."""
        ...

    async def close(self) -> None: ...


class Scheduler:
    def __init__(self, *, store: EventStore, policy: CommPolicy, runtime: RuntimeConfig,
                 runner: AgentRunner, clock: Clock, goal: str,
                 extensions: list[SchedulerExtension] | None = None):
        self.store, self.cfg = store, runtime
        self.runner, self.clock, self.goal = runner, clock, goal
        self.set_policy(policy)
        self.extensions = list(extensions or [])
        self.view = CommView()
        self.agents: dict[str, AgentState] = defaultdict(AgentState)
        self.human_inbox: dict[str, list[AttentionItem]] = defaultdict(list)
        self.timers: dict[str, tuple[str, datetime, str]] = {}
        self.cost_usd = 0.0
        self.started_at: datetime | None = None
        self.stop_requested = False
        self.paused: str | None = None
        self.stopped: str | None = None
        self.quiescent_logged = False
        self._msgs: deque[datetime] = deque()
        self._last_progress: datetime | None = None
        self._halt: dict[str, str] | None = None   # provider failure no activation can avoid
        self._cursor = 0
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()

    def set_policy(self, policy: CommPolicy) -> None:
        self.policy = policy
        self.gate = PolicyGate(policy, self.clock)

    # ---- folding ------------------------------------------------------------------------
    def catch_up(self) -> None:
        for ev in self.store.read_global(after_seq=self._cursor):
            self._cursor = ev.seq
            self._ingest(ev)

    def _ingest(self, ev: Event) -> None:
        self.view.apply(ev)
        p, t = ev.payload, ev.type
        agent = agent_of_activation_stream(ev.stream)
        if agent:
            st = self.agents[agent]
            if t == "activation_started":
                st.running = p["activation_id"]
                st.starts.append(ev.ts)
                self._deliver(st, (p.get("meta") or {}).get("attention", []))
            elif t == "incoming_added":
                self._deliver(st, (p.get("meta") or {}).get("attention", []))
            elif t == "activation_finished":
                self._finished(agent, st, ev)
        elif t == "run_started":
            self.started_at = ev.ts
        elif t == "run_stop_requested":
            self.stop_requested = True
        elif t == "breaker_tripped":
            self.paused, self._halt = p["kind"], None
        elif t == "breaker_reset":
            self.paused, self._halt = None, None
        elif t == "run_stopped":
            self.stopped = p["reason"]
        elif t == "run_resumed":
            self.stopped, self.stop_requested = None, False
        elif t == "agent_reset":
            st = self.agents[p["agent"]]
            st.faulted, st.errors, st.handoffs = False, 0, 0
        elif t == "timer_set":
            self.timers[p["timer_id"]] = (p["agent"], datetime.fromisoformat(p["due_ts"]),
                                          p["note"])
        elif t == "timer_fired":
            self.timers.pop(p["timer_id"], None)
        elif t == "message_posted":
            self._msgs.append(ev.ts)
        elif t in ("task_status_changed", "task_created"):
            self._last_progress = ev.ts
        if t in ("message_posted", "task_status_changed", "task_created", "timer_fired",
                 "activation_finished"):
            self.quiescent_logged = False
            for item in attention_for(ev, self.view, self.policy, self.gate):
                self._add_item(item)
        for ext in self.extensions:
            for item in ext.attention(ev, self.view):
                self.quiescent_logged = False
                self._add_item(item)

    def _add_item(self, item: AttentionItem) -> None:
        member = self.view.members.get(item.agent)
        if member is not None and member.kind == "human":
            self.human_inbox[item.agent].append(item)
        elif item.seq not in self.agents[item.agent].delivered:
            self.agents[item.agent].pending.append(item)

    @staticmethod
    def _deliver(st: AgentState, seqs: list[int]) -> None:
        st.delivered.update(seqs)
        st.pending = [i for i in st.pending if i.seq not in st.delivered]

    def _finished(self, agent: str, st: AgentState, ev: Event) -> None:
        p = ev.payload
        st.running = None
        st.last_finish = ev.ts
        self.cost_usd += float((p.get("totals") or {}).get("cost_usd", 0.0))
        outcome = p["outcome"]
        st.memory.append(MemoryEntry(ts=ev.ts.strftime("%m-%d %H:%M"), outcome=outcome,
                                     summary=(p.get("summary") or "")[:2000],
                                     reason=p.get("reason")))
        st.memory = st.memory[-self.cfg.memory_recent_activations:]
        if outcome == "error" and p.get("reason") in HALT_REASONS and not self.paused:
            self._halt = {"agent": agent, "reason": p["reason"]}
        if outcome == "error" and p.get("reason") != "cancelled":
            st.errors += 1
            if st.errors > self.cfg.error_retries:
                st.faulted = True
        else:
            st.errors = 0
        st.handoffs = st.handoffs + 1 if outcome in ("handoff", "budget_exhausted") else 0

    # ---- decisions ----------------------------------------------------------------------
    def _eligible_at(self, name: str, st: AgentState, waking: list[AttentionItem]) -> datetime:
        at = min(i.ts for i in waking) + timedelta(seconds=self.cfg.debounce_s)
        if any(i.kind == "retry" for i in waking) and st.last_finish:
            back = self.cfg.error_backoff_s
            at = max(at, st.last_finish + timedelta(seconds=back[min(st.errors, len(back)) - 1]))
        now = self.clock.now()
        hour = [s for s in st.starts if s > now - timedelta(hours=1)]
        if len(hour) >= self.cfg.rate.activations_per_agent_per_hour:
            at = max(at, hour[0] + timedelta(hours=1))
        return at

    def _waking(self, name: str, st: AgentState) -> list[AttentionItem]:
        st.pending = [i for i in st.pending if still_relevant(i, self.view)]
        waking = [i for i in st.pending if i.wakes]
        if st.handoffs >= self.cfg.rate.max_consecutive_handoffs:
            dropped = [i for i in waking if i.kind == "continue"]
            if dropped:
                st.pending = [i for i in st.pending if i.kind != "continue"]
                self.store.append(RUN, "handoff_limit", {"agent": name, "handoffs": st.handoffs},
                                  actor="system")
                waking = [i for i in waking if i.kind != "continue"]
        return waking

    def quiescent(self) -> bool:
        if self._tasks or any(ext.busy() for ext in self.extensions):
            return False
        # A wake-up only keeps the run alive while some work is still open; once every task
        # is closed, reminders set earlier ("check on T2") are stale.
        if self.timers and any(t.status not in TERMINAL for t in self.view.tasks.values()):
            return False
        for name, st in self.agents.items():
            if st.faulted or name not in self.view.members:
                continue
            if any(i.wakes for i in st.pending if still_relevant(i, self.view)):
                return False
        return True

    def _check_breakers(self, now: datetime) -> None:
        if self.paused:
            return
        if self._halt:
            self._trip("provider", self._halt)
            return
        b = self.cfg.budget
        elapsed = (now - self.started_at).total_seconds() if self.started_at else 0.0
        if self.cost_usd >= b.cost_usd or elapsed >= b.wall_clock_s:
            self._trip("budget", {"cost_usd": round(self.cost_usd, 4), "elapsed_s": elapsed})
            return
        s = self.cfg.rate.storm
        edge = now - timedelta(seconds=s.window_s)
        while self._msgs and self._msgs[0] < edge:
            self._msgs.popleft()
        since = [t for t in self._msgs if self._last_progress is None or t > self._last_progress]
        if len(self._msgs) > s.max_messages or len(since) > s.max_messages_without_progress:
            self._trip("storm", {"messages": len(self._msgs),
                                 "messages_since_progress": len(since),
                                 "window_s": s.window_s})

    def _trip(self, kind: str, detail: dict[str, Any]) -> None:
        self.store.append(RUN, "breaker_tripped", {"kind": kind, **detail}, actor="system")
        self.paused = kind
        # Batch runs have nobody to press resume, so any breaker ends them.
        if kind in ("budget", "provider") or self.cfg.on_quiescent == "stop":
            self.stop_requested = True

    # ---- activations --------------------------------------------------------------------
    def _poll_for(self, name: str):
        async def poll() -> list[tuple[str, dict]]:
            self.catch_up()
            st = self.agents[name]
            items = [i for i in self._waking(name, st)
                     if i.kind not in ("continue", "retry", "timer")]
            if not items:
                return []
            seqs = sorted({i.seq for i in items})
            text = render_incoming(name, items, self.view, self.cfg.briefing_messages_per_conv)
            self._deliver(st, seqs)
            return [(text, {"attention": seqs})]
        return poll

    def _start(self, name: str, st: AgentState) -> None:
        items = [i for i in st.pending if still_relevant(i, self.view)]
        seqs = sorted({i.seq for i in items})
        aid = new_id("act")
        briefing = render_briefing(
            member=self.view.members[name], items=items, view=self.view, gate=self.gate,
            goal=self.goal, memory=st.memory, per_conv=self.cfg.briefing_messages_per_conv,
            now=self.clock.now().strftime("%Y-%m-%d %H:%M UTC"),
            sections=[s for ext in self.extensions for s in ext.briefing(name)])
        st.running = aid
        self._deliver(st, seqs)
        coro = self.runner.run(name, aid, briefing, seqs, self._poll_for(name))
        self._spawn(name, aid, coro)

    def _spawn(self, name: str, aid: str, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks[aid] = task

        def done(t: asyncio.Task) -> None:
            self._tasks.pop(aid, None)
            if t.cancelled():
                exc: BaseException | None = None
            else:
                exc = t.exception()
            stream = activation_stream(name, aid)
            events = self.store.read(stream)
            if not any(e.type == "activation_finished" for e in events):
                self.store.append(stream, "activation_finished", {
                    "outcome": "error", "summary": f"{type(exc).__name__}: {exc}" if exc else "",
                    "reason": "cancelled" if t.cancelled() else "runner_error", "totals": {},
                    "steps": 0, "wall_s": 0, "compactions": 0, "artifacts": [], "claims": []},
                    actor="system")
            self._wake.set()

        task.add_done_callback(done)

    def _resume_unfinished(self) -> None:
        for name, st in self.agents.items():
            if st.running and st.running not in self._tasks:
                aid = st.running
                self._spawn(name, aid, self.runner.resume(name, aid, self._poll_for(name)))

    def _fire_timers(self, now: datetime) -> None:
        for tid, (agent, due, note) in list(self.timers.items()):
            if due <= now:
                self.store.append(f"agent/{agent}", "timer_fired",
                                  {"timer_id": tid, "agent": agent, "note": note},
                                  actor="system")

    def _next_deadline(self, now: datetime) -> float:
        waits = [MAX_WAIT_S]
        waits += [(due - now).total_seconds() for _, due, _ in self.timers.values()]
        return max(0.01, min(waits))

    def tick(self) -> None:
        now = self.clock.now()
        self._fire_timers(now)
        self._check_breakers(now)
        if self.stop_requested or self.paused:
            return
        candidates = []
        for name, st in list(self.agents.items()):
            member = self.view.members.get(name)
            if member is None or member.kind != "agent" or st.faulted or st.running:
                continue
            waking = self._waking(name, st)
            if waking:
                candidates.append((min(i.seq for i in waking), name, st, waking))
        for _, name, st, waking in sorted(candidates, key=lambda c: c[0]):
            if len(self._tasks) >= self.cfg.max_concurrent_activations:
                break
            if self._eligible_at(name, st, waking) > now:
                continue
            self._start(name, st)

    async def run(self) -> str:
        """Drive the team until it stops; returns the stop reason."""
        self.store.add_waiter(self._wake)
        try:
            self.catch_up()
            if self.stopped:
                # Resuming a stopped run continues it; with nothing new to do it goes quiescent
                # again at once. A tripped breaker must be reset first.
                if self.paused:
                    return self.stopped
                self.store.append(RUN, "run_resumed", {"after": self.stopped}, actor="human")
                self.catch_up()
            self._resume_unfinished()
            while True:
                self._wake.clear()
                self.catch_up()
                for ext in self.extensions:
                    ext.step()
                self.catch_up()
                done = next((r for ext in self.extensions
                             if (r := getattr(ext, "stop_reason", lambda: None)())), None)
                if done is not None:
                    # the run's own end condition holds: start nothing new, let running
                    # activations finish (their tail still gets recorded), then stop
                    if not self._tasks:
                        return self._stop(done)
                else:
                    self.tick()
                self.catch_up()
                if self.stop_requested and not self._tasks:
                    return self._stop("breaker:" + self.paused if self.paused else "human")
                if self.stop_requested:
                    for t in list(self._tasks.values()):
                        t.cancel()
                if not self.stop_requested and not self.paused and self.quiescent():
                    if not self.quiescent_logged:
                        self._log_quiescent()
                    if self.cfg.on_quiescent == "stop":
                        return self._stop("quiescent")
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           self._next_deadline(self.clock.now()))
                except TimeoutError:
                    pass
        finally:
            self.store.remove_waiter(self._wake)
            for t in list(self._tasks.values()):
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            for ext in self.extensions:
                await ext.close()
            await self.runner.close()

    def _log_quiescent(self) -> None:
        open_tasks = [t.id for t in self.view.tasks.values()
                      if t.status not in ("done", "cancelled")]
        faulted = [n for n, st in self.agents.items() if st.faulted]
        self.store.append(RUN, "run_quiescent", {"open_tasks": open_tasks, "faulted": faulted,
                                                 "cost_usd": round(self.cost_usd, 4)},
                          actor="system")
        self.quiescent_logged = True

    def _stop(self, reason: str) -> str:
        self.store.append(RUN, "run_stopped", {"reason": reason,
                                               "cost_usd": round(self.cost_usd, 4)},
                          actor="system")
        self.stopped = reason
        return reason
