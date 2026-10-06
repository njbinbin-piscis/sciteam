"""Runtime configuration and team files (docs/31-runtime.md §1, docs/30 §8)."""

from typing import Literal

from pydantic import BaseModel, Field


class StormConfig(BaseModel):
    window_s: float
    max_messages: int
    max_messages_without_progress: int


class RateConfig(BaseModel):
    activations_per_agent_per_hour: int
    max_consecutive_handoffs: int
    max_timers_per_agent: int
    storm: StormConfig


class RunBudget(BaseModel):
    cost_usd: float
    wall_clock_s: float


class TeamSandboxConfig(BaseModel):
    kind: Literal["local", "docker"]
    image: str | None = None
    network: str = "bridge"
    memory: str | None = "8g"
    cpus: float | None = 4
    init_commands: list[str] = Field(default_factory=list)
    workspace_mount: str = "/workspace"
    user: str | None = None               # "auto" = the host uid:gid (files stay user-owned)
    # docker -v specs mounted only into the institution's validation sandbox, never into the
    # members' sandboxes: evidence the members cannot read (host path relative to the cwd)
    validator_mounts: list[str] = Field(default_factory=list)


class RuntimeConfig(BaseModel):
    max_concurrent_activations: int
    debounce_s: float
    memory_recent_activations: int
    briefing_messages_per_conv: int
    deliver_during_activation: bool
    rate: RateConfig
    budget: RunBudget
    on_quiescent: Literal["stop", "wait"]
    error_retries: int
    error_backoff_s: list[float]
    sandbox: TeamSandboxConfig


class TeamMember(BaseModel):
    id: str
    kind: Literal["agent", "human"] = "agent"
    role: str = ""
    team: str = ""
    manager: str | None = None
    harness: str | None = None
    profile_ref: str | None = None
    attrs: dict[str, str] = Field(default_factory=dict)


class PresetRoom(BaseModel):
    name: str
    members: list[str]
    topic: str = ""


class PresetTask(BaseModel):
    key: str
    by: str
    title: str
    description: str = ""
    parent: str | None = None
    deps: list[str | dict] = Field(default_factory=list)   # keys of other preset tasks
    assignee: str | None = None
    reviewer: str | None = None
    estimate_h: float | None = None
    labels: list[str] = Field(default_factory=list)
    acceptance: str | None = None


class Kickoff(BaseModel):
    by: str
    to: str
    text: str


class TeamFile(BaseModel):
    name: str
    comm_policy: str
    institution: str | None = None        # constitution file (docs/40); None = no institution
    org: str | None = None                # organization manifest (docs/50); None = no org layer
    runtime: str
    goal_ref: str | None = None
    workspace_seed: str | None = None     # directory under configs/ copied into the workspace
    members: list[TeamMember]
    rooms: list[PresetRoom] = Field(default_factory=list)
    tasks: list[PresetTask] = Field(default_factory=list)
    kickoff: list[Kickoff] = Field(default_factory=list)   # first messages, sent through the Gate
