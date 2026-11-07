"""Build and run one member's activations (docs/31-runtime.md §3.3, §7)."""

import asyncio
import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sciteam.comm import COMM_TOOLS, CommClient, CommService
from sciteam.comm.board import unmet
from sciteam.harness import Harness, HarnessConfig
from sciteam.harness.context import skill_index
from sciteam.harness.loop import AwaitBudgetGrant, Checkpoint, PollIncoming
from sciteam.harness.memory import MemoryBook, MemoryView
from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.harness.tools.memory import MEMORY_TOOLS
from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.clock import Clock
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import EventStore
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.types import LlmProvider, ModelInfo, ToolSpec
from sciteam.providers.sandbox import DockerSandbox, DockerSandboxConfig, LocalSandbox
from sciteam.providers.sandbox.base import Sandbox
from sciteam.runtime.config import RuntimeConfig, library_mounts, member_mounts

LlmFactory = Callable[[str, ModelInfo], LlmProvider]   # (member id, model) -> provider


def activation_stream(agent: str, activation_id: str) -> str:
    return f"agent/{agent}/act/{activation_id}"


class TimerClient:
    def __init__(self, store: EventStore, agent: str, clock: Clock, max_timers: int,
                 active: Callable[[str], int]):
        self.store, self.agent, self.clock = store, agent, clock
        self.max_timers, self.active = max_timers, active

    def set(self, after_s: float, note: str) -> str:
        if self.active(self.agent) >= self.max_timers:
            raise ValueError(f"you already have {self.max_timers} pending wake-ups")
        tid = new_id("tmr")
        due = self.clock.now() + timedelta(seconds=after_s)
        self.store.append(f"agent/{self.agent}", "timer_set",
                          {"timer_id": tid, "agent": self.agent,
                           "due_ts": due.isoformat(), "note": note}, actor=self.agent)
        return tid


class ScheduleWakeupTool:
    readonly = False
    spec = ToolSpec(
        name="schedule_wakeup",
        description=("Ask to be woken up after after_s seconds with a note to yourself, e.g. to "
                     "check on a long-running job or on a teammate's progress."),
        parameters={"type": "object", "properties": {
            "after_s": {"type": "number"}, "note": {"type": "string"}},
            "required": ["after_s", "note"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        timers: TimerClient = ctx.extras["timers"]
        after = float(args["after_s"])
        if after < 1:
            return ToolResult(content="error: after_s must be at least 1", is_error=True)
        try:
            tid = timers.set(after, str(args["note"]))
        except ValueError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        return ToolResult(content=f"wake-up {tid} scheduled in {after:.0f}s")


TEAM_TOOLS = {**COMM_TOOLS, "schedule_wakeup": ScheduleWakeupTool()}


async def _remove_containers(label: str) -> None:
    """Containers a previous process of this run left behind; the scheduler lock makes them
    nobody else's."""
    ps = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", f"label={label}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await ps.communicate()
    ids = out.decode().split()
    if ids:
        rm = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", *ids,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await rm.wait()


class AgentRunner:
    def __init__(self, *, store: EventStore, blobs: BlobStore, comm: CommService,
                 configs: ConfigStore, models: dict[str, ModelInfo], llm_factory: LlmFactory,
                 runtime: RuntimeConfig, workspace: Path, clock: Clock,
                 active_timers: Callable[[str], int], run_name: str,
                 extra_tools: dict[str, Any] | None = None,
                 tool_extras: dict[str, Any] | None = None,
                 memory: MemoryBook | None = None):
        self.store, self.blobs, self.comm = store, blobs, comm
        self.memory = memory
        self.tools = {**TEAM_TOOLS, **MEMORY_TOOLS, **(extra_tools or {})}
        self.tool_extras = dict(tool_extras or {})
        self.configs, self.models, self.llm_factory = configs, models, llm_factory
        self.runtime, self.workspace, self.clock = runtime, workspace, clock
        self.active_timers = active_timers
        self.run_name = run_name
        self._sandboxes: dict[str, Sandbox] = {}
        self._llms: dict[tuple[str, str], LlmProvider] = {}
        self._sb_lock = asyncio.Lock()
        self._reaped = False

    def harness_config(self, agent: str) -> HarnessConfig:
        m = self.comm.view.members[agent]
        if not m.harness:
            raise ValueError(f"agent '{agent}' has no harness configured")
        return self.configs.load(m.harness, HarnessConfig)

    async def _sandbox(self, agent: str) -> Sandbox:
        async with self._sb_lock:
            sb = self._sandboxes.get(agent)
            if sb is not None:
                return sb
            sc = self.runtime.sandbox
            if sc.kind == "local":
                attrs = self.comm.view.members[agent].attrs
                if (sc.private_mount or attrs.get("workspace_access", "rw") != "rw"
                        or attrs.get("reads_private")):
                    raise ValueError("private_mount, workspace_access and reads_private "
                                     "need a docker team sandbox")
                sb = LocalSandbox(self.workspace, init_commands=sc.init_commands)
            else:
                if not sc.image:
                    raise ValueError("docker team sandbox needs an image")
                ws_label = str(self.workspace.resolve())
                if not self._reaped:
                    await _remove_containers(f"sciteam.workspace={ws_label}")
                    self._reaped = True
                sb = DockerSandbox(DockerSandboxConfig(
                    image=sc.image, workdir=sc.workspace_mount, network=sc.network,
                    memory=sc.memory, cpus=sc.cpus, init_commands=sc.init_commands,
                    mounts=[*member_mounts(sc, self.workspace, agent,
                                           self.comm.view.members[agent].attrs),
                            *library_mounts(sc, self.configs.base)],
                    labels={"sciteam.run": self.run_name, "sciteam.agent": agent,
                            "sciteam.workspace": ws_label},
                    user=f"{os.getuid()}:{os.getgid()}" if sc.user == "auto" else sc.user,
                    env={"HOME": "/tmp"}, platform=None))
            await sb.start()
            self._sandboxes[agent] = sb
            return sb

    def _llm(self, agent: str, info: ModelInfo) -> LlmProvider:
        key = (agent, info.name)
        if key not in self._llms:
            self._llms[key] = self.llm_factory(agent, info)
        return self._llms[key]

    async def _harness(self, agent: str, activation_id: str, poll: PollIncoming,
                       budget_grant: AwaitBudgetGrant | None = None,
                       checkpoint: Checkpoint | None = None) -> Harness:
        cfg = self.harness_config(agent)
        info = self.models[cfg.model]
        member = self.comm.view.members[agent]
        prompt = self.configs.load_text(cfg.system_prompt_ref)
        if member.profile:
            prompt += f"\n\n## Your role: @{agent}\n{member.profile.strip()}"
        sc = self.runtime.sandbox
        prompt += skill_index(self.configs, root=sc.library_mount if sc.kind == "docker"
                              else str(self.configs.base.resolve()))
        tools = {n: t for n, t in self.tools.items() if n in cfg.tools}
        extras = {**self.tool_extras, "comm": CommClient(self.comm, agent, activation_id),
                  "timers": TimerClient(self.store, agent, self.clock,
                                        self.runtime.rate.max_timers_per_agent,
                                        self.active_timers)}
        if self.memory is not None:
            extras["memory"] = MemoryView(self.memory, agent, member.role, project=self.run_name,
                                          activation=activation_id)
        return Harness(cfg, model_info=info, llm=self._llm(agent, info),
                       sandbox=await self._sandbox(agent), store=self.store, blobs=self.blobs,
                       system_prompt=prompt, clock=self.clock, extra_tools=tools,
                       tool_extras=extras,
                       poll_incoming=poll if self.runtime.deliver_during_activation else None,
                       await_budget_grant=budget_grant, checkpoint=checkpoint,
                       finish_check=self._finish_check(agent)
                       if self.runtime.finish_checks_held_tasks else None)

    def _outstanding(self, agent: str) -> list[Any]:
        """Tasks the agent can work on now: assigned and startable, in progress with no
        submission of its own waiting on a check, or in review by it with none of its own
        reproductions still replaying."""
        tasks = self.comm.view.tasks
        inst = self.tool_extras.get("institution")
        submitted = {str(p.act.params.get("task")) for p in inst.inst.pending.values()
                     if p.act.actor == agent} if inst is not None else set()
        replaying = {c.task_id for c in inst.inst.claims.values()
                     if c.actor == agent and c.status == "requested"} if inst is not None else set()
        out = []
        for t in tasks.values():
            if t.reviewer == agent and t.status == "in_review" and t.id not in replaying:
                out.append(t)
                continue
            if t.assignee != agent:
                continue
            if t.status == "assigned" and not unmet(t, tasks, "start"):
                out.append(t)
            elif t.status == "in_progress" and not {t.id, t.short} & submitted:
                out.append(t)
        return out

    def _finish_check(self, agent: str):
        """completed means every task the agent can work on is submitted or done; the first
        blocked/need_input while such work remains is sent back once."""
        sent_back = False

        async def check(args: dict[str, Any], ctx: Any) -> str | None:
            nonlocal sent_back
            outcome = args.get("outcome")
            if outcome == "handoff":
                return None
            held = self._outstanding(agent)
            if outcome != "completed" and (sent_back or not held):
                return None
            if not held:
                return None
            sent_back = sent_back or outcome != "completed"
            names = ", ".join(f"{t.short} [{t.status}] {t.title}" for t in held[:6])
            return (f"you hold work you can do now: {names}. Work on it in this activation: "
                    f"start it, write and run the code, check and commit the outputs, then "
                    f"submit it for review; a task in review by you: reproduce it, record and "
                    f"attest your claims, then approve or reject it. If it truly cannot "
                    f"proceed, tell whoever can "
                    f"unblock you and finish with blocked; use handoff if your own work needs "
                    f"another activation.")
        return check

    async def run(self, agent: str, activation_id: str, briefing: str, attention: list[int],
                  poll: PollIncoming, *, budget_grant: AwaitBudgetGrant | None = None,
                  checkpoint: Checkpoint | None = None) -> None:
        h = await self._harness(agent, activation_id, poll, budget_grant, checkpoint)
        await h.run(briefing, stream=activation_stream(agent, activation_id),
                    activation_id=activation_id, meta={"agent": agent, "attention": attention})

    async def resume(self, agent: str, activation_id: str, poll: PollIncoming, *,
                     budget_grant: AwaitBudgetGrant | None = None,
                     checkpoint: Checkpoint | None = None) -> None:
        h = await self._harness(agent, activation_id, poll, budget_grant, checkpoint)
        await h.resume(activation_stream(agent, activation_id))

    async def close(self) -> None:
        for sb in self._sandboxes.values():
            await sb.close()
        for llm in self._llms.values():
            close = getattr(llm, "aclose", None)
            if close is not None:
                await close()
        self._sandboxes.clear()
        self._llms.clear()
