"""Build and run one member's activations (docs/31-runtime.md §3.3, §7)."""

import asyncio
import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sciteam.comm import COMM_TOOLS, CommClient, CommService
from sciteam.harness import Harness, HarnessConfig
from sciteam.harness.loop import PollIncoming
from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.clock import Clock
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import EventStore
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.types import LlmProvider, ModelInfo, ToolSpec
from sciteam.providers.sandbox import DockerSandbox, DockerSandboxConfig, LocalSandbox
from sciteam.providers.sandbox.base import Sandbox
from sciteam.runtime.config import RuntimeConfig

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


class AgentRunner:
    def __init__(self, *, store: EventStore, blobs: BlobStore, comm: CommService,
                 configs: ConfigStore, models: dict[str, ModelInfo], llm_factory: LlmFactory,
                 runtime: RuntimeConfig, workspace: Path, clock: Clock,
                 active_timers: Callable[[str], int], run_name: str,
                 extra_tools: dict[str, Any] | None = None,
                 tool_extras: dict[str, Any] | None = None):
        self.store, self.blobs, self.comm = store, blobs, comm
        self.tools = {**TEAM_TOOLS, **(extra_tools or {})}
        self.tool_extras = dict(tool_extras or {})
        self.configs, self.models, self.llm_factory = configs, models, llm_factory
        self.runtime, self.workspace, self.clock = runtime, workspace, clock
        self.active_timers = active_timers
        self.run_name = run_name
        self._sandboxes: dict[str, Sandbox] = {}
        self._llms: dict[tuple[str, str], LlmProvider] = {}
        self._sb_lock = asyncio.Lock()

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
                sb = LocalSandbox(self.workspace, init_commands=sc.init_commands)
            else:
                if not sc.image:
                    raise ValueError("docker team sandbox needs an image")
                sb = DockerSandbox(DockerSandboxConfig(
                    image=sc.image, workdir=sc.workspace_mount, network=sc.network,
                    memory=sc.memory, cpus=sc.cpus, init_commands=sc.init_commands,
                    mounts=[f"{self.workspace.resolve()}:{sc.workspace_mount}"],
                    labels={"sciteam.run": self.run_name, "sciteam.agent": agent},
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

    async def _harness(self, agent: str, activation_id: str, poll: PollIncoming) -> Harness:
        cfg = self.harness_config(agent)
        info = self.models[cfg.model]
        member = self.comm.view.members[agent]
        prompt = self.configs.load_text(cfg.system_prompt_ref)
        if member.profile:
            prompt += f"\n\n## Your role: @{agent}\n{member.profile.strip()}"
        tools = {n: t for n, t in self.tools.items() if n in cfg.tools}
        extras = {**self.tool_extras, "comm": CommClient(self.comm, agent, activation_id),
                  "timers": TimerClient(self.store, agent, self.clock,
                                        self.runtime.rate.max_timers_per_agent,
                                        self.active_timers)}
        return Harness(cfg, model_info=info, llm=self._llm(agent, info),
                       sandbox=await self._sandbox(agent), store=self.store, blobs=self.blobs,
                       system_prompt=prompt, clock=self.clock, extra_tools=tools,
                       tool_extras=extras,
                       poll_incoming=poll if self.runtime.deliver_during_activation else None)

    async def run(self, agent: str, activation_id: str, briefing: str, attention: list[int],
                  poll: PollIncoming) -> None:
        h = await self._harness(agent, activation_id, poll)
        await h.run(briefing, stream=activation_stream(agent, activation_id),
                    activation_id=activation_id, meta={"agent": agent, "attention": attention})

    async def resume(self, agent: str, activation_id: str, poll: PollIncoming) -> None:
        h = await self._harness(agent, activation_id, poll)
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
