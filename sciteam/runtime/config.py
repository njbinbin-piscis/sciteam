"""Runtime configuration and team files (docs/31-runtime.md §1, docs/30 §8)."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sciteam.harness.memory import MemoryConfig


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
    """Whole-run limits; 0 disables one. In an interactive run (`on_quiescent: wait`) a reached
    limit pauses the team and asks the owner; `auto_continue` answers for them: `ask` never,
    `up_to` at most `auto_continue_max` times per run, `always` every time."""
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    tokens: int = 0
    auto_continue: Literal["ask", "up_to", "always"] = "ask"
    auto_continue_max: int = 0


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
    # member x's private directory <run>/private/x appears at <private_mount>/x in every
    # container that mounts it: read-write for x, read-only for members whose `reads_private`
    # names x and for the validation sandbox (which mounts all of them) (docs/31 §7)
    private_mount: str | None = None
    # the run's improvement-library content (skills, helper scripts, memory) appears read-only
    # under this path in every member's container
    library_mount: str = "/library"


def private_root(workspace: Path) -> Path:
    return workspace.parent / "private"


def member_mounts(sc: TeamSandboxConfig, workspace: Path, agent: str,
                  attrs: dict[str, str] | None = None) -> list[str]:
    """Member attributes: `workspace_access: ro` mounts the shared workspace read-only;
    `reads_private: a,b` adds those members' private directories read-only."""
    attrs = attrs or {}
    access = attrs.get("workspace_access", "rw")
    if access not in ("rw", "ro"):
        raise ValueError(f"workspace_access must be rw or ro, got {access!r}")
    mounts = [f"{workspace.resolve()}:{sc.workspace_mount}" + (":ro" if access == "ro" else "")]
    incoming = workspace / "data" / "incoming"
    if workspace.is_dir():
        incoming.mkdir(parents=True, exist_ok=True)
    if incoming.is_dir():
        mounts.append(f"{incoming.resolve()}:{sc.workspace_mount.rstrip('/')}/data/incoming:ro")
    peers = [x.strip() for x in attrs.get("reads_private", "").split(",") if x.strip()]
    if peers and not sc.private_mount:
        raise ValueError("reads_private needs private_mount")
    if sc.private_mount:
        root, base = private_root(workspace), sc.private_mount.rstrip("/")
        own = root / agent
        own.mkdir(parents=True, exist_ok=True)
        mounts.append(f"{own.resolve()}:{base}/{agent}")
        for peer in peers:
            if peer == agent:
                raise ValueError(f"{agent}: reads_private lists itself")
            d = root / peer
            d.mkdir(parents=True, exist_ok=True)
            mounts.append(f"{d.resolve()}:{base}/{peer}:ro")
    return mounts


LIBRARY_DIRS = ("skills", "scripts", "memory")


def library_mounts(sc: TeamSandboxConfig, configs_base: Path) -> list[str]:
    """The run's frozen library content, read-only at <library_mount>/<dir>."""
    base = sc.library_mount.rstrip("/")
    return [f"{(configs_base / d).resolve()}:{base}/{d}:ro" for d in LIBRARY_DIRS
            if (configs_base / d).is_dir()]


def validator_private_mounts(sc: TeamSandboxConfig, workspace: Path,
                             member: str | None = None) -> list[str]:
    """A replay sees only the private directory of the member whose claim it replays; every
    other validation sees none."""
    if not sc.private_mount or not member:
        return []
    own = private_root(workspace) / member
    own.mkdir(parents=True, exist_ok=True)
    return [f"{own.resolve()}:{sc.private_mount.rstrip('/')}/{member}:ro"]


class OfflineConfig(BaseModel):
    """What the team does while a decision waits for the owner (a review gate, a spec to
    confirm, a budget request): keep working on what does not depend on it, or pause; and
    after timeout_s (0 = never), keep waiting or stop the run until the owner resumes it."""
    while_waiting: Literal["continue", "pause"] = "continue"
    timeout_s: float = 0
    on_timeout: Literal["wait", "stop"] = "wait"


class RuntimeConfig(BaseModel):
    max_concurrent_activations: int
    debounce_s: float
    memory_recent_activations: int
    briefing_messages_per_conv: int
    deliver_during_activation: bool
    # finish is sent back while the agent holds a task it can work on now
    finish_checks_held_tasks: bool = False
    rate: RateConfig
    budget: RunBudget
    on_quiescent: Literal["stop", "wait"]
    error_retries: int
    error_backoff_s: list[float]
    sandbox: TeamSandboxConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    offline: OfflineConfig = Field(default_factory=OfflineConfig)


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
