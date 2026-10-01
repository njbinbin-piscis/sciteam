"""pi (@earendil-works/pi-coding-agent) baseline runner for SWE-bench.

pi runs on the host in `-p --mode json` (print/headless) mode with its default system prompt, compaction
and tool set (read, bash, edit, write). The `docker_tools.ts` extension re-implements those four tools'
I/O via `docker exec` into the per-instance container, which stays on `--network none`.

Usage:
  .venv/bin/python bench/baselines/run_pi.py --ids-file X --run-dir runs/pi_xxx --concurrency N \
      --model deepseek-flash
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from common import (
    Result,
    extract_patch,
    load_api_key,
    main,
    model_info,
    peak_cost_usd,
    remove_container,
    start_container,
    task_text,
)

HERE = Path(__file__).resolve().parent
PI_PKG = Path(os.environ.get(
    "PI_PKG", "/home/uuu/Projects/ATLAS/paper/lab/tools/pi/node_modules/@earendil-works/pi-coding-agent"))
PI_CLI = PI_PKG / "dist" / "cli.js"
EXTENSION = HERE / "pi" / "docker_tools.ts"
PROVIDER = "sciteam-deepseek"
TOOLS = "read,bash,edit,write"


def pi_version() -> str:
    return json.loads((PI_PKG / "package.json").read_text())["version"]


def models_json(model: str) -> dict[str, Any]:
    info = model_info(model)
    return {"providers": {PROVIDER: {
        "baseUrl": info["base_url"],
        "api": "openai-completions",
        "apiKey": "$DEEPSEEK_API_KEY",
        "compat": {
            "thinkingFormat": "deepseek",
            "supportsReasoningEffort": True,
            "requiresReasoningContentOnAssistantMessages": True,
        },
        "models": [{
            "id": info["wire_model"],
            "name": model,
            "reasoning": True,
            "input": ["text"],
            "contextWindow": info["context_window"],
            "maxTokens": info["max_output_tokens"],
            "thinkingLevelMap": {"minimal": None, "low": "low", "medium": None, "high": "high",
                                 "xhigh": None, "max": "max"},
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }],
    }}}


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


async def run_one(inst: dict[str, Any], args: argparse.Namespace, run_dir: Path) -> Result:
    iid = inst["instance_id"]
    info = model_info(args.model)
    work = run_dir / "work" / iid
    shutil.rmtree(work, ignore_errors=True)
    agent_dir, cwd, tmp = work / "agent", work / "cwd", work / "tmp"
    for d in (agent_dir, cwd, tmp):
        d.mkdir(parents=True)
    (agent_dir / "models.json").write_text(json.dumps(models_json(args.model), indent=2))
    session_dir = run_dir / "sessions" / iid
    shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True)
    log_path = run_dir / "logs" / f"{iid}.pi.jsonl"
    err_path = run_dir / "logs" / f"{iid}.pi.stderr"

    container = _sanitize(f"sciteam-pi-{run_dir.name}-{iid}")[:120]
    await remove_container(container)
    t0 = time.monotonic()
    res = Result(instance_id=iid, status="infra_error")
    proc = None
    try:
        await start_container(iid, "pi", run_dir.name, name=container)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work),
            "PI_CODING_AGENT_DIR": str(agent_dir), "TMPDIR": str(tmp), "PI_OFFLINE": "1",
            "DEEPSEEK_API_KEY": load_api_key(),
            "SCITEAM_PI_CONTAINER": container, "SCITEAM_PI_WORKDIR": "/testbed",
            "SCITEAM_PI_PAYLOAD_AUDIT": str(run_dir / "logs" / f"{iid}.pi.payload_audit.jsonl"),
        }
        argv = ["node", str(PI_CLI), "-p", "--mode", "json",
                "--no-extensions", "-e", str(EXTENSION), "--no-skills", "--no-prompt-templates",
                "--no-themes", "--no-context-files", "--offline", "--tools", TOOLS,
                "--provider", PROVIDER, "--model", info["wire_model"],
                "--thinking", args.reasoning_effort, "--session-dir", str(session_dir),
                task_text(inst)]
        stats: dict[str, Any] = {"turns": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                                 "reasoning": 0, "compactions": 0, "tool_calls": 0, "tool_errors": 0,
                                 "last_stop": None, "last_error": None, "thinking_chars": 0,
                                 "agent_end": False}
        with err_path.open("wb") as ferr:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=ferr, start_new_session=True,
                limit=64 << 20)
            status = await _pump(proc, log_path, stats, args, t0)
        res.exit_code = proc.returncode
        prompt = stats["input"] + stats["cacheRead"] + stats["cacheWrite"]
        res.steps = stats["turns"]
        res.prompt_tokens, res.completion_tokens = prompt, stats["output"]
        res.cache_hit_tokens, res.reasoning_tokens = stats["cacheRead"], stats["reasoning"]
        res.compactions = stats["compactions"]
        res.cost_usd = round(peak_cost_usd(args.model, prompt, stats["cacheRead"], stats["output"]), 4)
        res.cost_note = "peak-price upper bound from pi usage"
        res.extra = {k: stats[k] for k in ("tool_calls", "tool_errors", "last_stop", "thinking_chars",
                                           "agent_end")}
        if status == "completed" and (stats["last_stop"] == "error" or not stats["agent_end"]
                                      or proc.returncode != 0):
            status = "agent_error"
            res.error = (stats["last_error"]
                         or f"pi exit code {proc.returncode}; see {err_path.name}")[-3000:]
        res.status = status
        patch = await extract_patch(container)
        (run_dir / "patches" / f"{iid}.diff").write_text(patch)
        res.patch_chars = len(patch)
    finally:
        if proc is not None and proc.returncode is None:
            _killpg(proc)
            await proc.wait()
        await remove_container(container)
        shutil.rmtree(work, ignore_errors=True)
        res.wall_s = round(time.monotonic() - t0, 1)
    return res


def _killpg(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _pump(proc: asyncio.subprocess.Process, log_path: Path, stats: dict[str, Any],
                args: argparse.Namespace, t0: float) -> str:
    """Stream pi's JSON events to disk, keep tallies, and enforce wall/step limits."""
    assert proc.stdout
    deadline = t0 + args.wall_limit_s
    status = "completed"
    with log_path.open("w") as flog:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "wall_timeout"
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=min(remaining, 60))
            except TimeoutError:
                continue
            if not line:
                break
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                flog.write(line.decode(errors="replace"))
                continue
            t = ev.get("type")
            if t == "message_update":
                # Deltas are redundant with message_end; keep only the thinking volume.
                ame = ev.get("assistantMessageEvent") or {}
                if ame.get("type") == "thinking_delta":
                    stats["thinking_chars"] += len(ame.get("delta") or "")
                continue
            flog.write(line.decode(errors="replace"))
            if t == "message_end":
                msg = ev.get("message") or {}
                if msg.get("role") == "assistant":
                    stats["turns"] += 1
                    u = msg.get("usage") or {}
                    for k in ("input", "output", "cacheRead", "cacheWrite", "reasoning"):
                        stats[k] += int(u.get(k) or 0)
                    stats["last_stop"] = msg.get("stopReason")
                    if msg.get("errorMessage"):
                        stats["last_error"] = msg["errorMessage"]
            elif t == "tool_execution_end":
                stats["tool_calls"] += 1
                stats["tool_errors"] += bool(ev.get("isError"))
            elif t == "compaction_end":
                stats["compactions"] += 1
            elif t == "agent_end":
                stats["agent_end"] = True
            if stats["turns"] >= args.max_steps:
                status = "step_limit"
                break
    if status != "completed":
        _killpg(proc)
    await proc.wait()
    return status


if __name__ == "__main__":
    main("pi", run_one,
         model_name_fn=lambda a: f"pi-{pi_version()}@{a.model}",
         config=lambda a: {"pi_version": pi_version(), "pi_tools": TOOLS,
                           "extension": str(EXTENSION), "thinking": a.reasoning_effort})
