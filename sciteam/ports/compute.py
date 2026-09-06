"""Compute port: submit/status/logs/cancel/fetch with idempotent job ids.

Deliberately scheduler-agnostic. A local-container adapter, an SSH adapter, or
a batch-scheduler adapter all satisfy this protocol. Every job carries enough
provenance (image digest, command, env, seed, io hashes, host snapshot, cost)
to be replayed and audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ComputeJobSpec:
    """A unit of remote/local compute. ``job_id`` doubles as an idempotency key."""

    job_id: str
    image: str
    command: list[str]
    inputs: dict[str, str] = field(default_factory=dict)  # local path -> remote rel path
    outputs: tuple[str, ...] = ()  # remote rel paths to fetch back
    env: dict[str, str] = field(default_factory=dict)
    gpus: int = 0
    timeout_s: int = 3600
    workdir: str = "."
    seed: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "image": self.image,
            "command": list(self.command),
            "inputs": dict(self.inputs),
            "outputs": list(self.outputs),
            "env_keys": sorted(self.env),  # values scrubbed: never serialise secrets
            "gpus": self.gpus,
            "timeout_s": self.timeout_s,
            "workdir": self.workdir,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class ComputeJobResult:
    job_id: str
    state: JobState
    exit_code: int | None = None
    stdout_hash: str = ""
    stderr_hash: str = ""
    outputs: dict[str, str] = field(default_factory=dict)  # remote rel path -> content hash
    image_digest: str = ""
    started_at: str = ""
    ended_at: str = ""
    host_snapshot: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)  # gpu_hours, wall_s, ...
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": str(self.state),
            "exit_code": self.exit_code,
            "stdout_hash": self.stdout_hash,
            "stderr_hash": self.stderr_hash,
            "outputs": dict(self.outputs),
            "image_digest": self.image_digest,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "host_snapshot": dict(self.host_snapshot),
            "cost": dict(self.cost),
            "detail": self.detail,
        }


@runtime_checkable
class ComputePort(Protocol):
    def submit(self, spec: ComputeJobSpec) -> str: ...

    def status(self, job_id: str) -> ComputeJobResult: ...

    def logs(self, job_id: str) -> str: ...

    def cancel(self, job_id: str) -> None: ...

    def fetch(self, job_id: str, name: str, dest: str) -> str: ...
