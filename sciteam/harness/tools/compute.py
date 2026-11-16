"""Heavy or GPU work on a compute node: `run_on_node` ships input files to a node, runs one
command there, brings the named outputs back and writes a job record into the workspace under
`jobs/<job>/` (command, node, hashes of inputs and outputs, environment hash, log). The record is
the evidence a conclusion rests on; `rerun_job` replays it so a reviewer can check it.

The node side is a `ComputeBackend`, supplied by the product."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from typing import Any, Protocol

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.types import ToolSpec

JOBS = "jobs"


@dataclass
class JobSpec:
    command: str
    inputs: dict[str, bytes] = field(default_factory=dict)   # relative path -> content
    outputs: list[str] = field(default_factory=list)         # relative paths to bring back
    gpus: int = 0
    timeout_s: float = 3600
    node: str = ""
    job_id: str = ""


@dataclass
class JobResult:
    exit_code: int | None
    output: str
    outputs: dict[str, bytes] = field(default_factory=dict)
    duration_s: float = 0.0
    timed_out: bool = False
    node_id: str = ""
    node_name: str = ""
    env_hash: str = ""
    gpu_indices: list[int] = field(default_factory=list)


class ComputeBackend(Protocol):
    def available(self) -> list[dict[str, Any]]: ...
    async def run(self, spec: JobSpec) -> JobResult: ...


class ComputeError(Exception):
    """A message fit for the agent to read."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel(path: str) -> str:
    p = posixpath.normpath(str(path).lstrip("/"))
    if p.startswith("..") or p in ("", "."):
        raise ComputeError(f"path must stay inside the workspace: {path!r}")
    return p


def _backend(ctx: ToolContext) -> ComputeBackend:
    b = ctx.extras.get("compute")
    if b is None:
        raise ComputeError("no compute node is connected to this project")
    return b


class RunOnNodeTool:
    readonly = False
    spec = ToolSpec(
        name="run_on_node",
        description=(
            "Run one shell command on a compute node (a machine with GPUs or many CPUs, set up by "
            "the owner) instead of in this sandbox. `inputs`: workspace files to upload. `outputs`: "
            "files the command writes (relative to its working directory) that come back into "
            "`jobs/<job>/out/`. `gpus`: how many GPUs the command needs; the job waits for free ones "
            "and sees only its own. The command runs in the project's virtual environment on the node. "
            "A job record lands in `jobs/<job>/job.json`; cite it as evidence for results that depend "
            "on it."),
        parameters={"type": "object", "properties": {
            "command": {"type": "string"},
            "inputs": {"type": "array", "items": {"type": "string"}},
            "outputs": {"type": "array", "items": {"type": "string"}},
            "gpus": {"type": "integer", "minimum": 0},
            "timeout_s": {"type": "number"},
            "node": {"type": "string", "description": "node id; omit to let the studio choose"}},
            "required": ["command"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            backend = _backend(ctx)
            job = new_id("job")[-10:]
            inputs: dict[str, bytes] = {}
            for p in args.get("inputs") or []:
                rel = _rel(p)
                inputs[rel] = await ctx.sandbox.read_file(rel)
            spec = JobSpec(command=str(args["command"]), inputs=inputs,
                           outputs=[_rel(p) for p in args.get("outputs") or []],
                           gpus=int(args.get("gpus") or 0),
                           timeout_s=min(float(args.get("timeout_s") or 3600), ctx.remaining_wall_s),
                           node=str(args.get("node") or ""), job_id=job)
            res = await backend.run(spec)
        except ComputeError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        return await _record(ctx, spec, res, job)


async def _record(ctx: ToolContext, spec: JobSpec, res: JobResult, job: str) -> ToolResult:
    base = f"{JOBS}/{job}"
    for rel, data in spec.inputs.items():
        await ctx.sandbox.write_file(f"{base}/in/{rel}", data)
    for rel, data in res.outputs.items():
        await ctx.sandbox.write_file(f"{base}/out/{rel}", data)
    await ctx.sandbox.write_file(f"{base}/log.txt", res.output.encode())
    manifest = {
        "job": job, "command": spec.command, "node": res.node_id, "node_name": res.node_name,
        "gpus": spec.gpus, "gpu_indices": res.gpu_indices, "env_hash": res.env_hash,
        "exit_code": res.exit_code, "timed_out": res.timed_out, "duration_s": round(res.duration_s, 1),
        "inputs": {k: sha(v) for k, v in spec.inputs.items()},
        "outputs": {k: sha(v) for k, v in res.outputs.items()},
        "wanted_outputs": spec.outputs, "timeout_s": spec.timeout_s}
    await ctx.sandbox.write_file(f"{base}/job.json", json.dumps(manifest, indent=1).encode())
    ok = res.exit_code == 0 and not res.timed_out
    tail = "\n".join(res.output.strip().splitlines()[-30:])
    missing = [p for p in spec.outputs if p not in res.outputs]
    head = (f"job {job} on {res.node_name or res.node_id}: "
            + ("timed out" if res.timed_out else f"exit {res.exit_code}")
            + f", {res.duration_s:.0f}s; record {base}/job.json"
            + (f"; outputs missing: {missing}" if missing else ""))
    return ToolResult(content=f"{head}\n{tail}", is_error=not ok,
                      meta={"job": job, "node": res.node_id, "manifest": f"{base}/job.json"})


class RerunJobTool:
    readonly = False
    spec = ToolSpec(
        name="rerun_job",
        description=(
            "Replay a recorded job (`jobs/<job>/job.json`) on the same node with the same inputs "
            "and command, and report whether every output has the same content hash. Use it to "
            "check a conclusion that rests on a node job."),
        parameters={"type": "object", "properties": {"job": {"type": "string"}},
                    "required": ["job"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            backend = _backend(ctx)
            old = _rel(str(args["job"]))
            base = f"{JOBS}/{posixpath.basename(old)}"
            try:
                m = json.loads((await ctx.sandbox.read_file(f"{base}/job.json")).decode())
            except Exception:  # noqa: BLE001
                raise ComputeError(f"no job record at {base}/job.json") from None
            inputs = {k: await ctx.sandbox.read_file(f"{base}/in/{k}") for k in m["inputs"]}
            if any(sha(v) != m["inputs"][k] for k, v in inputs.items()):
                raise ComputeError("the recorded inputs changed since the job ran")
            job = new_id("job")[-10:]
            spec = JobSpec(command=m["command"], inputs=inputs, outputs=m["wanted_outputs"],
                           gpus=m["gpus"], timeout_s=m["timeout_s"], node=m["node"], job_id=job)
            res = await backend.run(spec)
        except ComputeError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        same = {k: sha(res.outputs.get(k, b"")) == v for k, v in m["outputs"].items()}
        done = await _record(ctx, spec, res, job)
        verdict = "reproduced" if same and all(same.values()) and res.exit_code == 0 else "differs"
        bad = [k for k, v in same.items() if not v]
        return ToolResult(content=f"{verdict}: replay of {m['job']} is {job}"
                          + (f"; outputs that differ: {bad}" if bad else "") + "\n" + done.content,
                          is_error=verdict != "reproduced", meta={**done.meta, "verdict": verdict})


COMPUTE_TOOLS = {"run_on_node": RunOnNodeTool(), "rerun_job": RerunJobTool()}
