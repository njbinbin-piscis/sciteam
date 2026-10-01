"""Shared plumbing for the pi / Hermes SWE-bench baseline runners.

Container setup, task text and patch extraction mirror `sciteam/bench/swebench.py` so the only
variable between arms is the agent harness itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sciteam.bench.swebench import (  # noqa: E402
    DIFF_CMD,
    HISTORY_SCRUB,
    TASK_TEMPLATE,
    image_for,
    load_instances,
)

DEFAULT_DATASET = REPO / "bench" / "swebench" / "verified.jsonl"
MODELS_YAML = REPO / "configs" / "models.yaml"

# Our harness has a `finish` tool; the baselines end their run by simply stopping.
_FINISH_SENTENCE = "When the fix is implemented and verified, call finish."
assert _FINISH_SENTENCE in TASK_TEMPLATE
BASELINE_TASK_TEMPLATE = TASK_TEMPLATE.replace(
    _FINISH_SENTENCE, "When the fix is implemented and verified, stop.")

INIT_COMMANDS = ["git config --global --add safe.directory /testbed", HISTORY_SCRUB]
# Infrastructure failures are retried on resume; agent-side outcomes are final.
INFRA_STATUSES = {"infra_error"}

WALL_LIMIT_S = 3 * 3600


def task_text(inst: dict[str, Any]) -> str:
    return BASELINE_TASK_TEMPLATE.format(problem_statement=inst["problem_statement"])


def load_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DEEPSEEK_API_KEY not found in environment or .env")


def model_info(model: str) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(MODELS_YAML.read_text())[model]


def peak_cost_usd(model: str, prompt: int, cache_hit: int, completion: int) -> float:
    """Upper-bound cost at peak pricing (the baselines do not track per-call time of day)."""
    p = model_info(model)["pricing_usd_per_mtok"]["peak"]
    miss = max(prompt - cache_hit, 0)
    return (cache_hit * p["cache_hit"] + miss * p["cache_miss"] + completion * p["output"]) / 1e6


async def run_cmd(*argv: str, timeout: float = 300, input: bytes | None = None,
                  env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def start_container(iid: str, agent: str, run_name: str, *, name: str,
                          extra_labels: dict[str, str] | None = None) -> str:
    image = image_for(iid)
    code, out = await run_cmd("docker", "image", "inspect", image, timeout=60)
    if code != 0:
        code, out = await run_cmd("docker", "pull", "--platform", "linux/amd64", image, timeout=3600)
        if code != 0:
            raise RuntimeError(f"docker pull failed: {out[-2000:]}")
    labels = {"sciteam.baseline": agent, "sciteam.run": run_name, "sciteam.instance": iid,
              **(extra_labels or {})}
    args = ["docker", "run", "-d", "--name", name, "--network", "none", "-w", "/testbed",
            "--memory", "16g", "--cpus", "4", "--platform", "linux/amd64"]
    for k, v in labels.items():
        args += ["--label", f"{k}={v}"]
    args += [image, "sleep", "infinity"]
    code, out = await run_cmd(*args, timeout=300)
    if code != 0:
        raise RuntimeError(f"docker run failed: {out[-2000:]}")
    for c in INIT_COMMANDS:
        code, out = await run_cmd("docker", "exec", "-w", "/testbed", name, "bash", "-c", c, timeout=900)
        if code != 0:
            raise RuntimeError(f"init command failed: {c}: {out[-1000:]}")
    return name


async def extract_patch(container: str) -> str:
    # Recover the container if the agent's teardown stopped it.
    await run_cmd("docker", "start", container, timeout=120)
    code, out = await run_cmd("docker", "exec", "-w", "/testbed", container, "bash", "-c", DIFF_CMD,
                              timeout=300)
    if code != 0:
        raise RuntimeError(f"git diff failed: {out[-1000:]}")
    return out if out.endswith("\n") or not out else out + "\n"


async def remove_container(container: str) -> None:
    try:
        await run_cmd("docker", "rm", "-f", container, timeout=120)
    except Exception:
        pass


async def remove_labeled(agent: str, run_name: str | None = None) -> None:
    filt = ["--filter", f"label=sciteam.baseline={agent}"]
    if run_name:
        filt += ["--filter", f"label=sciteam.run={run_name}"]
    code, out = await run_cmd("docker", "ps", "-aq", *filt, timeout=60)
    ids = out.split()
    if code == 0 and ids:
        await run_cmd("docker", "rm", "-f", *ids, timeout=300)


@dataclass
class Result:
    instance_id: str
    status: str  # completed | wall_timeout | agent_error | infra_error
    wall_s: float = 0.0
    steps: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    cost_note: str | None = None
    compactions: int | None = None
    patch_chars: int = 0
    exit_code: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def common_args(agent: str) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=f"Run the {agent} baseline on SWE-bench instances.")
    ap.add_argument("--ids-file", type=Path, help="file with one instance_id per line")
    ap.add_argument("--ids", nargs="*", help="instance ids (alternative to --ids-file)")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--reasoning-effort", default="max")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--wall-limit-s", type=float, default=WALL_LIMIT_S)
    ap.add_argument("--max-steps", type=int, default=3000)
    return ap.parse_args()


RunOne = Callable[[dict[str, Any], argparse.Namespace, Path], Awaitable[Result]]


def main(agent: str, run_one: RunOne, model_name_fn: Callable[[argparse.Namespace], str],
         config: Callable[[argparse.Namespace], dict[str, Any]]) -> None:
    args = common_args(agent)
    asyncio.run(_main(agent, args, run_one, model_name_fn(args), config(args)))


async def _main(agent: str, args: argparse.Namespace, run_one: RunOne, model_name: str,
                config: dict[str, Any]) -> None:
    run_dir: Path = args.run_dir.resolve()
    (run_dir / "patches").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    ids: list[str] | None = None
    if args.ids_file:
        ids = [x.strip() for x in args.ids_file.read_text().splitlines() if x.strip()]
    elif args.ids:
        ids = list(args.ids)
    results_path = run_dir / "results.jsonl"
    latest: dict[str, dict] = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                latest[r["instance_id"]] = r  # last record wins; reclassification appends
    done = {iid for iid, r in latest.items() if r["status"] not in INFRA_STATUSES}
    instances = [i for i in load_instances(args.dataset, ids) if i["instance_id"] not in done]
    (run_dir / "config.json").write_text(json.dumps({
        "agent": agent, "model": args.model, "reasoning_effort": args.reasoning_effort,
        "model_name_or_path": model_name, "dataset": str(args.dataset),
        "wall_limit_s": args.wall_limit_s, "max_steps": args.max_steps,
        "task_template": BASELINE_TASK_TEMPLATE, "n_pending": len(instances), "n_done": len(done),
        **config}, indent=2))
    print(f"[{agent}] {len(instances)} pending, {len(done)} already done -> {run_dir}", flush=True)
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    halted = asyncio.Event()

    async def one(inst: dict[str, Any]) -> None:
        iid = inst["instance_id"]
        async with sem:
            if halted.is_set():
                return
            t0 = time.monotonic()
            try:
                res = await run_one(inst, args, run_dir)
            except Exception as e:
                res = Result(instance_id=iid, status="infra_error", wall_s=time.monotonic() - t0,
                             error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}")
            if is_quota_error(res.error) or quota_in_logs(run_dir, iid):
                res.status = "infra_error"
                if not halted.is_set():
                    halted.set()
                    print(f"[{agent}] HALT: provider quota exhausted; no new instances will "
                          "start. Rerun the same command after topping up.", flush=True)
            async with lock:
                with results_path.open("a") as f:
                    f.write(json.dumps(asdict(res)) + "\n")
            print(f"[{time.strftime('%H:%M:%S')}] {iid}: {res.status} steps={res.steps} "
                  f"{res.wall_s / 60:.1f}min patch={res.patch_chars} cost<=${res.cost_usd or 0:.3f}"
                  f"{' err=' + res.error.splitlines()[0][:200] if res.error else ''}", flush=True)

    try:
        await asyncio.gather(*(one(i) for i in instances))
    finally:
        await remove_labeled(agent, run_dir.name)
    write_predictions(run_dir, model_name)


QUOTA_MARKERS = ("Insufficient Balance", "insufficient_quota", "exceeded your current quota",
                 "billing, credits, or account entitlement is exhausted")


def is_quota_error(error: str | None) -> bool:
    return bool(error) and (error.startswith("402") or any(m in error for m in QUOTA_MARKERS))


def quota_in_logs(run_dir: Path, iid: str) -> bool:
    """Agents may only report provider billing errors in their own stdout/stderr."""
    for p in (run_dir / "logs").glob(f"{iid}.*.std*"):
        if is_quota_error(p.read_text(errors="replace")[-20000:]):
            return True
    return False


def write_predictions(run_dir: Path, model_name: str) -> Path:
    """Only instances whose latest result is not an infra error are submitted for scoring."""
    latest: dict[str, dict] = {}
    results_path = run_dir / "results.jsonl"
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                latest[r["instance_id"]] = r
    valid = {iid for iid, r in latest.items() if r["status"] not in INFRA_STATUSES}
    preds = run_dir / "preds.jsonl"
    with preds.open("w") as f:
        for p in sorted((run_dir / "patches").glob("*.diff")):
            if p.stem in valid:
                f.write(json.dumps({"instance_id": p.stem, "model_name_or_path": model_name,
                                    "model_patch": p.read_text()}) + "\n")
    return preds
