"""Hermes Agent baseline runner for SWE-bench.

Hermes runs on the host in one-shot mode (`hermes -z PROMPT`) with its default system prompt, compaction
and tools, using its own `docker` terminal backend. We pre-create the per-instance container exactly like
our harness (`--network none`, same image/limits) and label it the way Hermes labels its persistent
containers, so Hermes *attaches* to it instead of creating one with its own mounts (its fresh containers
mount tmpfs over /root, hiding the image's conda activation in ~/.bashrc).

Usage:
  .venv/bin/python bench/baselines/run_hermes.py --ids-file X --run-dir runs/hermes_xxx --concurrency N \
      --model deepseek-flash
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from common import (
    REPO,
    Result,
    extract_patch,
    image_for,
    load_api_key,
    main,
    model_info,
    peak_cost_usd,
    remove_container,
    run_cmd,
    start_container,
    task_text,
)

HERE = Path(__file__).resolve().parent
HERMES_SRC = REPO / "third_party" / "hermes-agent"
HERMES_BIN = Path(os.environ.get("HERMES_BIN", HERE / ".hermes-venv" / "bin" / "hermes"))
# Hermes' default "cli" toolsets minus the ones that execute on the host with network access or need
# a display/API keys we do not provide (web, browser, vision, image_gen, tts, computer_use, connections,
# cronjob). Everything kept here executes inside the container or is purely in-process.
TOOLSETS = "terminal,file,code_execution,todo,memory,skills,session_search,delegation,clarify"


def hermes_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERMES_SRC), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def _label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:63] or "unknown"


def hermes_labels(shared_key: str) -> dict[str, str]:
    """Labels Hermes' DockerEnvironment filters on when looking for a reusable container
    (tools/environments/docker.py `_container_identity` / `_find_reusable_container`)."""
    digest = hashlib.sha256(shared_key.encode()).hexdigest()[:12]
    return {"hermes-agent": "1", "hermes-task-id": _label(f"shared:{shared_key}"),
            "hermes-profile": f"{_label(shared_key)[:50]}-{digest}", "hermes-egress": "off"}


def hermes_config(model: str, iid: str, shared_key: str, run_name: str) -> dict[str, Any]:
    info = model_info(model)
    return {
        "model": {"default": info["wire_model"], "provider": "deepseek", "base_url": info["base_url"],
                  "context_length": info["context_window"]},
        "terminal": {
            "backend": "docker", "cwd": "/testbed", "docker_image": image_for(iid),
            "docker_network": False, "container_persistent": True,
            "docker_persist_across_processes": True, "docker_shared_container_key": shared_key,
            "docker_orphan_reaper": False,
            # Only used if Hermes ignores our container and creates its own (detected afterwards).
            "docker_extra_args": ["--label", "sciteam.baseline=hermes",
                                  "--label", f"sciteam.run={run_name}",
                                  "--label", f"sciteam.instance={iid}",
                                  "--label", "sciteam.hermes_created=1"],
        },
    }


async def run_one(inst: dict[str, Any], args: argparse.Namespace, run_dir: Path) -> Result:
    iid = inst["instance_id"]
    info = model_info(args.model)
    work = run_dir / "work" / iid
    await _rmtree_root_owned(work, image_for(iid))
    home, cwd = work / "hermes_home", work / "cwd"
    home.mkdir(parents=True)
    cwd.mkdir()
    shared_key = "sciteam-" + hashlib.sha256(f"{run_dir}/{iid}".encode()).hexdigest()[:16]
    cfg = hermes_config(args.model, iid, shared_key, run_dir.name)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    usage_path = run_dir / "logs" / f"{iid}.hermes.usage.json"
    out_path = run_dir / "logs" / f"{iid}.hermes.stdout"
    err_path = run_dir / "logs" / f"{iid}.hermes.stderr"
    usage_path.unlink(missing_ok=True)

    container = _label(f"sciteam-hermes-{run_dir.name}-{iid}")[:120]
    await remove_container(container)
    t0 = time.monotonic()
    res = Result(instance_id=iid, status="infra_error")
    proc = None
    try:
        await start_container(iid, "hermes", run_dir.name, name=container,
                              extra_labels=hermes_labels(shared_key))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work),
               "HERMES_HOME": str(home), "DEEPSEEK_API_KEY": load_api_key(),
               "HERMES_ACCEPT_HOOKS": "1", "LANG": "C.UTF-8"}
        if args.audit_requests:
            env["HERMES_DUMP_REQUESTS"] = "1"
        argv = [str(HERMES_BIN), "-z", task_text(inst), "--provider", "deepseek",
                "-m", info["wire_model"], "--reasoning", args.reasoning_effort,
                "--toolsets", TOOLSETS, "--usage-file", str(usage_path)]
        status = "completed"
        with out_path.open("wb") as fout, err_path.open("wb") as ferr:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=fout, stderr=ferr,
                start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=args.wall_limit_s)
            except TimeoutError:
                status = "wall_timeout"
                _killpg(proc)
                await proc.wait()
        res.exit_code = proc.returncode
        usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
        _fill_usage(res, usage, args.model)
        if status == "completed" and (proc.returncode != 0 or usage.get("failure")):
            status = "agent_error"
            res.error = (str(usage.get("failure") or "")
                         or f"hermes exit code {proc.returncode}; see {err_path.name}")[-3000:]
        res.status = status

        created = await _hermes_created_containers(run_dir.name, iid)
        res.extra["attached_to_prepared_container"] = not created
        patch_src = created[0] if created else container
        if created:
            res.extra["deviation"] = "hermes created its own container (tmpfs /root, its security flags)"
        if args.audit_requests:
            res.extra["request_audit"] = _summarize_request_dumps(
                home, run_dir / "logs" / f"{iid}.hermes.payload_audit.jsonl")
        patch = await extract_patch(patch_src)
        (run_dir / "patches" / f"{iid}.diff").write_text(patch)
        res.patch_chars = len(patch)
    finally:
        if proc is not None and proc.returncode is None:
            _killpg(proc)
            await proc.wait()
        for c in await _hermes_created_containers(run_dir.name, iid, include_probe=True):
            await remove_container(c)
        await remove_container(container)
        _keep_session_artifacts(home, run_dir / "sessions" / iid)
        await _rmtree_root_owned(work, image_for(iid))
        res.wall_s = round(time.monotonic() - t0, 1)
    return res


def _fill_usage(res: Result, usage: dict[str, Any], model: str) -> None:
    if not usage:
        return
    res.steps = usage.get("api_calls")
    # Auxiliary calls (title generation, compaction summaries, ...) hit the same model; count them too.
    parts = [usage, usage.get("auxiliary") or {}]

    def total(key: str) -> int:
        return sum(int(p.get(key) or 0) for p in parts)

    prompt = total("input_tokens") + total("cache_read_tokens") + total("cache_write_tokens")
    res.prompt_tokens = prompt
    res.completion_tokens = total("output_tokens")
    res.cache_hit_tokens = total("cache_read_tokens")
    res.reasoning_tokens = total("reasoning_tokens")
    by_task = (usage.get("auxiliary") or {}).get("by_task") or {}
    res.compactions = sum(int(v.get("api_calls") or 0) for k, v in by_task.items() if "compress" in k)
    res.cost_usd = round(peak_cost_usd(model, prompt, res.cache_hit_tokens, res.completion_tokens), 4)
    res.cost_note = "peak-price upper bound from hermes usage file (main + auxiliary calls)"
    res.extra["hermes_usage"] = usage


async def _hermes_created_containers(run_name: str, iid: str, *, include_probe: bool = False) -> list[str]:
    """Containers Hermes created itself for this instance. Its system-prompt builder always spins up a
    short-lived `prompt-backend-probe` container (runs `uname` only, then force-removed); that one is
    not a deviation for the agent's commands."""
    code, out = await run_cmd("docker", "ps", "-a", "--filter", "label=sciteam.hermes_created=1",
                              "--filter", f"label=sciteam.run={run_name}",
                              "--filter", f"label=sciteam.instance={iid}",
                              "--format", '{{.ID}}\t{{.Label "hermes-task-id"}}', timeout=60)
    if code != 0:
        return []
    rows = [ln.split("\t", 1) + [""] for ln in out.splitlines() if ln.strip()]
    return [r[0] for r in rows if include_probe or "prompt-backend-probe" not in r[1]]


async def _rmtree_root_owned(path: Path, image: str) -> None:
    """Hermes' probe container bind-mounts dirs under HERMES_HOME and leaves root-owned files there."""
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        await run_cmd("docker", "run", "--rm", "--network", "none", "-v", f"{path}:/w", image,
                      "sh", "-c", "rm -rf /w/* /w/.[!.]*", timeout=300)
        shutil.rmtree(path, ignore_errors=True)


def _summarize_request_dumps(home: Path, out: Path) -> dict[str, Any]:
    """Condense HERMES_DUMP_REQUESTS dumps into per-request rows (no message content)."""
    rows = []
    for f in sorted(home.rglob("request_dump_*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        req = d.get("request") or d
        body = req.get("body") or req.get("kwargs") or req
        msgs = body.get("messages") or []
        asst = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]
        extra = body.get("extra_body") or {}
        rows.append({
            "file": f.name, "model": body.get("model"),
            "thinking": body.get("thinking", extra.get("thinking")),
            "reasoning_effort": body.get("reasoning_effort", extra.get("reasoning_effort")),
            "tool_choice": body.get("tool_choice"), "n_messages": len(msgs), "n_assistant": len(asst),
            "n_assistant_reasoning_nonblank": sum(
                1 for m in asst
                if isinstance(m.get("reasoning_content"), str) and m["reasoning_content"].strip()),
            "n_assistant_reasoning_missing": sum(1 for m in asst if "reasoning_content" not in m),
        })
        f.unlink()
    with out.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return {"n_requests": len(rows),
            "max_assistant": max((r["n_assistant"] for r in rows), default=0),
            "any_reasoning_missing": any(r["n_assistant_reasoning_missing"] for r in rows)}


def _keep_session_artifacts(home: Path, dest: Path) -> None:
    """Keep Hermes' session store/transcripts for auditing; the rest of HERMES_HOME is disposable."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("state.db", "sessions", "logs"):
        src = home / name
        if src.is_dir():
            shutil.copytree(src, dest / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("request_dump_*"))
        elif src.exists():
            shutil.copy2(src, dest / name)


def _killpg(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    import sys

    audit = "--audit-requests" in sys.argv
    if audit:
        sys.argv.remove("--audit-requests")

    def _cfg(a: argparse.Namespace) -> dict[str, Any]:
        a.audit_requests = audit
        return {"hermes_commit": hermes_commit(), "hermes_toolsets": TOOLSETS, "audit_requests": audit,
                "step_limit_note": "hermes -z has no step cap (max_iterations unlimited); only the wall "
                                   "limit is enforced"}

    main("hermes", run_one, model_name_fn=lambda a: f"hermes-{hermes_commit()}@{a.model}", config=_cfg)
