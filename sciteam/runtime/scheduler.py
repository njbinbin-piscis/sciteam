"""Event-driven scheduler: no rounds; whoever has attention gets an activation
(docs/31-runtime.md §3–§6). All scheduler state is a fold of the event store."""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel

from sciteam.comm.model import TERMINAL, Member
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
    agent_of_stream,
    attention_for,
    still_relevant,
)
from sciteam.runtime.briefing import MemoryEntry, render_briefing, render_incoming
from sciteam.runtime.config import RuntimeConfig

RUN = "run"
MAX_WAIT_S = 1.0
APPROVAL_POLL_S = 0.5
# Errors the provider recovers from by itself (rate limits, outages): the agent retries at
# the longest backoff for as long as they last and is never faulted for them.
TRANSIENT_REASONS = frozenset({"provider_unavailable"})


def faults(errors: int, reason: str | None, retries: int) -> tuple[int, bool]:
    """Consecutive error count and faulted flag after an activation ends in error."""
    if reason in TRANSIENT_REASONS:
        return errors + 1, False
    return errors + 1, errors + 1 > retries


def memory_limit(member: Member | None, default: int) -> int:
    """Activation summaries carried into the next briefing; a member's `memory_activations`
    attribute overrides the runtime default, and "0" makes every activation start blank
    (docs/31 §5)."""
    raw = member.attrs.get("memory_activations") if member is not None else None
    return default if raw is None else max(0, int(raw))


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
        # the agents allowed to act; None = all (a replay fork restricts it to the seat tried)
        self.only: set[str] | None = None
        self.view = CommView()
        self.agents: dict[str, AgentState] = defaultdict(AgentState)
        self.human_inbox: dict[str, list[AttentionItem]] = defaultdict(list)
        self.timers: dict[str, tuple[str, datetime, str]] = {}
        self.cost_usd = 0.0
        self.started_at: datetime | None = None
        self.paused_s = 0.0
        self._stopped_at: datetime | None = None
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
        # budget: tokens are counted as model calls return, not when activations finish
        self.tokens_used = 0
        self.run_extra: dict[str, float] = defaultdict(float)   # run limits granted on top
        self.requests: dict[str, dict[str, Any]] = {}          # pause_id -> approval request
        self.decisions: dict[str, dict[str, Any]] = {}         # pause_id -> owner/auto decision
        self.auto_grants = 0
        self.handoff_requests: dict[str, str] = {}             # agent -> pending pause_id
        self.handoff_declined: set[str] = set()
        self.request_ts: dict[str, float] = {}
        self.offline = runtime.offline.model_copy()
        self.resumed_ts = 0.0
        self.stop_reason: str | None = None
        self._waiting_logged = False

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
        if t in ("model_returned", "summary_usage") and agent_of_stream(ev.stream):
            u = p.get("usage") or {}
            self.tokens_used += int(u.get("prompt_tokens") or 0) + \
                int(u.get("completion_tokens") or 0)
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
        elif t == "budget_approval_requested":
            self.requests[p["pause_id"]] = p
            self.request_ts[p["pause_id"]] = ev.ts.timestamp()
            if p.get("scope") == "handoffs":
                self.handoff_requests[p["agent"]] = p["pause_id"]
        elif t == "budget_decided":
            self._fold_decision(p)
        elif t == "budget_settings_changed":
            self.cfg = self.cfg.model_copy(update={"budget": self.cfg.budget.model_copy(
                update={k: v for k, v in p.items() if k in type(self.cfg.budget).model_fields})})
        elif t == "run_stopped":
            self.stopped = p["reason"]
            self._stopped_at = ev.ts
        elif t == "run_resumed":
            if self._stopped_at is not None and ev.ts > self._stopped_at:
                self.paused_s += (ev.ts - self._stopped_at).total_seconds()
            self._stopped_at = None
            self.stopped, self.stop_requested = None, False
            self.resumed_ts, self.stop_reason = ev.ts.timestamp(), None
        elif t == "offline_settings_changed":
            self.offline = self.offline.model_copy(update=p)
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
        keep = memory_limit(self.view.members.get(agent), self.cfg.memory_recent_activations)
        st.memory = st.memory[-keep:] if keep > 0 else []
        if outcome == "error" and p.get("reason") in HALT_REASONS and not self.paused:
            self._halt = {"agent": agent, "reason": p["reason"]}
        if outcome == "error" and p.get("reason") != "cancelled":
            st.errors, faulted = faults(st.errors, p.get("reason"), self.cfg.error_retries)
            st.faulted = st.faulted or faulted
        else:
            st.errors = 0
        st.handoffs = st.handoffs + 1 if outcome in ("handoff", "budget_exhausted") else 0
        if st.handoffs == 0:
            self.handoff_declined.discard(agent)

    def _fold_decision(self, p: dict[str, Any]) -> None:
        pid = p["pause_id"]
        self.decisions[pid] = p
        if p.get("actor") == "auto_policy":
            self.auto_grants += 1
        req = self.requests.get(pid) or {}
        granted = p.get("decision") == "grant"
        scope = req.get("scope")
        if scope == "run":
            if granted:
                for dim, amount in (p.get("add") or req.get("default_add") or {}).items():
                    self.run_extra[dim] += float(amount)
                if self.paused == "budget":
                    self.paused = None
            else:
                self.stop_requested = True
        elif scope == "handoffs":
            agent = req["agent"]
            self.handoff_requests.pop(agent, None)
            if granted:
                self.agents[agent].handoffs = 0
            else:
                self.handoff_declined.add(agent)

    # ---- budget approvals ---------------------------------------------------------------
    def _auto_allows(self) -> bool:
        b = self.cfg.budget
        if b.auto_continue == "always":
            return True
        return b.auto_continue == "up_to" and self.auto_grants < b.auto_continue_max

    def _request(self, scope: str, detail: dict[str, Any]) -> str:
        """Ask the owner for more budget; the auto policy answers at once when it allows."""
        pid = detail.get("pause_id") or new_id("bp")
        self.store.append(RUN, "budget_approval_requested",
                          {**detail, "pause_id": pid, "scope": scope}, actor="system")
        self.catch_up()
        if self._auto_allows():
            self.decide(pid, "grant", actor="auto_policy")
        return pid

    def decide(self, pause_id: str, decision: str, *, actor: str = "owner",
               add: dict[str, float] | None = None) -> None:
        if pause_id in self.decisions:
            return
        self.store.append(RUN, "budget_decided", {"pause_id": pause_id, "decision": decision,
                                                  "actor": actor, "add": add or {}},
                          actor=actor)
        self.catch_up()
        self._wake.set()

    def _grant_for(self, name: str):
        async def grant(pause: dict[str, Any]) -> dict[str, Any] | None:
            pid = pause["pause_id"]
            self.catch_up()
            if pid not in self.requests and pid not in self.decisions:
                if self.cfg.on_quiescent == "stop" and not self._auto_allows():
                    return None          # a batch run has nobody to ask
                self._request("activation", {
                    "pause_id": pid, "agent": name, "binding": pause.get("binding"),
                    "exhausted": pause.get("exhausted"), "used": pause.get("used"),
                    "limits": pause.get("limits"),
                    "activation_id": pause.get("activation_id")})
            while pid not in self.decisions:
                await asyncio.sleep(APPROVAL_POLL_S)
                self.catch_up()
            d = self.decisions[pid]
            if d.get("decision") != "grant":
                return None
            return {"actor": d.get("actor", "owner"), "add": d.get("add") or {}}
        return grant

    async def _checkpoint(self) -> float:
        """Activations wait here while the whole run is paused for budget."""
        self.catch_up()
        if self.paused != "budget":
            return 0.0
        t0 = self.clock.monotonic()
        while self.paused == "budget" and not self.stop_requested:
            await asyncio.sleep(APPROVAL_POLL_S)
            self.catch_up()
        return max(0.0, self.clock.monotonic() - t0)

    # ---- decisions ----------------------------------------------------------------------
    def _eligible_at(self, name: str, st: AgentState, waking: list[AttentionItem]) -> datetime:
        at = min(i.ts for i in waking) + timedelta(seconds=self.cfg.debounce_s)
        if any(i.kind == "retry" for i in waking) and st.last_finish and st.errors:
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
            interactive = self.cfg.on_quiescent == "wait" or self._auto_allows()
            if dropped and interactive and name not in self.handoff_declined:
                # ask before continuing further instead of silently dropping the work
                if name not in self.handoff_requests:
                    self._request("handoffs", {"agent": name, "handoffs": st.handoffs,
                                               "limit": self.cfg.rate.max_consecutive_handoffs})
                if st.handoffs >= self.cfg.rate.max_consecutive_handoffs:
                    waking = [i for i in waking if i.kind != "continue"]
            elif dropped:
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
            if st.faulted or name not in self.view.members or not self._may_act(name):
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
        elapsed = (
            max(0.0, (now - self.started_at).total_seconds() - self.paused_s)
            if self.started_at
            else 0.0
        )
        lim = {"tokens": float(b.tokens), "cost": b.cost_usd, "wall_clock": b.wall_clock_s}
        used = {"tokens": float(self.tokens_used), "cost": self.cost_usd, "wall_clock": elapsed}
        over = [d for d in lim if lim[d] > 0 and used[d] >= lim[d] + self.run_extra[d]]
        if over:
            pid = new_id("bp")
            self._trip("budget", {"cost_usd": round(self.cost_usd, 4), "elapsed_s": elapsed,
                                  "tokens": self.tokens_used, "exhausted": over,
                                  "pause_id": pid})
            if self.cfg.on_quiescent == "wait":
                self._request("run", {
                    "pause_id": pid, "binding": over[0], "exhausted": over,
                    "used": used, "limits": {d: lim[d] + self.run_extra[d] for d in lim},
                    "default_add": {d: lim[d] for d in over}})
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
        # Batch runs have nobody to press resume, so any breaker ends them. An interactive run
        # pauses at its budget and asks the owner (see _request).
        if kind == "provider" or self.cfg.on_quiescent == "stop":
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
        coro = self.runner.run(name, aid, briefing, seqs, self._poll_for(name),
                               budget_grant=self._grant_for(name), checkpoint=self._checkpoint)
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

    def _may_act(self, name: str) -> bool:
        return self.only is None or name in self.only

    def _resume_unfinished(self) -> None:
        for name, st in self.agents.items():
            if st.running and st.running not in self._tasks and self._may_act(name):
                aid = st.running
                self._spawn(name, aid, self.runner.resume(
                    name, aid, self._poll_for(name), budget_grant=self._grant_for(name),
                    checkpoint=self._checkpoint))

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

    def _release_open_requests(self) -> None:
        """Requests asked under a stricter policy are granted once the policy allows them."""
        for pid in list(self.requests):
            if pid in self.decisions or not self._auto_allows():
                continue
            self.decide(pid, "grant", actor="auto_policy")

    def owner_waits(self) -> list[tuple[str, float]]:
        """Decisions waiting for the owner, with when each was asked."""
        out = [(f"budget:{pid}", self.request_ts.get(pid, 0.0)) for pid in self.requests
               if pid not in self.decisions]
        for ext in self.extensions:
            fn = getattr(ext, "owner_waits", None)
            if fn is not None:
                out += fn()
        return out

    def _offline(self, now: datetime) -> bool:
        """Apply the offline policy; True = start nothing this tick."""
        waits = self.owner_waits()
        if not waits:
            self._waiting_logged = False
            return False
        off = self.offline
        since = max(min(ts for _, ts in waits), self.resumed_ts)
        waited = now.timestamp() - since
        if off.timeout_s and waited >= off.timeout_s and off.on_timeout == "stop":
            self.store.append(RUN, "owner_wait_timed_out", {
                "waits": sorted(k for k, _ in waits), "waited_s": round(waited, 1)},
                actor="system")
            self.stop_requested, self.stop_reason = True, "owner_offline"
            return True
        if off.while_waiting == "pause":
            if not self._waiting_logged:
                self.store.append(RUN, "team_waiting_for_owner",
                                  {"waits": sorted(k for k, _ in waits)}, actor="system")
                self._waiting_logged = True
            return True
        return False

    def tick(self) -> None:
        now = self.clock.now()
        self._release_open_requests()
        self._fire_timers(now)
        self._check_breakers(now)
        if self.stop_requested or self.paused:
            return
        if self._offline(now):
            return
        candidates = []
        for name, st in list(self.agents.items()):
            member = self.view.members.get(name)
            if member is None or member.kind != "agent" or st.faulted or st.running \
                    or not self._may_act(name):
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
                    return self._stop(self.stop_reason or (
                        "breaker:" + self.paused if self.paused else "human"))
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
