"""SWE-bench driver (docs/20-harness.md §11.1).

One activation per instance in a fresh container from the official prebuilt image; the patch is
`git diff` of /testbed after the activation. Scoring is done separately by the official
swebench 4.1.0 harness on the predictions file this driver writes.
"""

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sciteam.harness import Harness, HarnessConfig
from sciteam.harness.tools.base import ToolContext
from sciteam.kernel import BlobStore, ConfigStore, EventStore, new_id
from sciteam.providers.llm import load_models, make_provider
from sciteam.providers.llm.types import HALT_REASONS
from sciteam.providers.sandbox import DockerSandbox, DockerSandboxConfig

TASK_TEMPLATE = """<issue>
{problem_statement}
</issue>

The repository is checked out at /testbed at the commit where this issue was reported; the project's Python environment is already activated in your shell. Resolve the issue by editing the non-test source files in /testbed so that the described behaviour is fixed. Keep reproduction and scratch files under /tmp, outside the repository. When the fix is implemented and verified, call finish."""  # noqa: E501

DIFF_CMD = "cd /testbed && git add -A && git diff --cached --no-color HEAD"
# Some SWE-bench images keep the upstream history after the base commit (tags, other refs,
# the clone's reflog), so the reference fix is reachable with `git log --all`. Before the agent
# starts, keep only the current branch, drop remotes and reflogs, and prune what is unreachable.
HISTORY_SCRUB = (
    "cd /testbed && git tag -l | xargs -r git tag -d >/dev/null"
    " && git for-each-ref --format='%(refname)'"
    " | grep -vxF \"$(git symbolic-ref -q HEAD || echo HEAD)\" | xargs -r -n1 git update-ref -d"
    " && git remote | xargs -r -n1 git remote remove"
    " && git reflog expire --expire=now --all && git gc --prune=now --quiet"
    " && test \"$(git rev-list --all --reflog | wc -l)\" = \"$(git rev-list HEAD | wc -l)\""
)


def image_for(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


class InstanceResult(BaseModel):
    instance_id: str
    outcome: str
    reason: str | None
    patch_chars: int
    cost_usd: float
    steps: int
    wall_s: float
    compactions: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    stream: str
    error: str | None = None


def load_instances(path: Path, ids: list[str] | None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if ids:
        by_id = {r["instance_id"]: r for r in rows}
        return [by_id[i] for i in ids]
    return rows


_ARTIFACT = re.compile(r"(^|/)(build|dist|\.eggs|\.tox|\.pytest_cache|__pycache__)/|\.egg-info/|\.py[co]$")


def drop_new_artifacts(patch: str) -> str:
    """Remove newly added build/cache artifacts (e.g. build/lib/** after `setup.py build`).

    Only file sections that create a file are dropped; edits to tracked files are kept as is.
    """
    sections = re.split(r"(?m)^(?=diff --git )", patch)
    kept = []
    for s in sections:
        m = re.match(r"diff --git a/(\S+) b/\S+\n(new file mode)?", s)
        if m and m.group(2) and _ARTIFACT.search(m.group(1)):
            continue
        kept.append(s)
    return "".join(kept)


async def _patch(sb: DockerSandbox) -> str:
    code, out = await sb.exec_once(DIFF_CMD, timeout=300)
    if code != 0:
        raise RuntimeError(f"git diff failed: {out[-1000:]}")
    out = drop_new_artifacts(out)
    return out if out.endswith("\n") or not out else out + "\n"


async def run_instance(inst: dict[str, Any], *, cfg: HarnessConfig, configs: ConfigStore,
                       models_path: Path, run_dir: Path, store: EventStore, blobs: BlobStore,
                       log) -> InstanceResult:
    iid = inst["instance_id"]
    model_info = load_models(models_path)[cfg.model]
    llm = make_provider(model_info)
    sb = DockerSandbox(DockerSandboxConfig(
        image=image_for(iid), workdir="/testbed", network=cfg.sandbox.network,
        init_commands=["source /opt/miniconda3/bin/activate testbed", "git config --global "
                       "--add safe.directory /testbed", HISTORY_SCRUB],
        labels={"sciteam.run": run_dir.name, "sciteam.instance": iid},
    ))

    async def finish_check(args: dict[str, Any], ctx: ToolContext) -> str | None:
        if args.get("outcome") != "completed":
            return None
        if not (await _patch(sb)).strip():
            return "there are no changes in /testbed; the issue is not resolved yet"
        return None

    stream = f"swebench/{iid}/{new_id('try')}"
    t0 = time.monotonic()
    try:
        await sb.start()
        h = Harness(cfg, model_info=model_info, llm=llm, sandbox=sb, store=store, blobs=blobs,
                    system_prompt=configs.load_text(cfg.system_prompt_ref),
                    finish_check=finish_check)
        task = TASK_TEMPLATE.format(problem_statement=inst["problem_statement"])
        res = await h.run(task, stream=stream, meta={"instance_id": iid, "bench": "swebench"})
        patch = await _patch(sb)
        (run_dir / "patches").mkdir(exist_ok=True)
        (run_dir / "patches" / f"{iid}.diff").write_text(patch)
        out = InstanceResult(
            instance_id=iid, outcome=res.outcome, reason=res.reason, patch_chars=len(patch),
            cost_usd=res.totals.cost_usd, steps=res.steps, wall_s=res.wall_s,
            compactions=res.compactions, prompt_tokens=res.totals.prompt_tokens,
            completion_tokens=res.totals.completion_tokens,
            cache_hit_tokens=res.totals.cache_hit_tokens, stream=stream)
    except Exception as e:
        out = InstanceResult(instance_id=iid, outcome="driver_error", reason=type(e).__name__,
                             patch_chars=0, cost_usd=0, steps=0, wall_s=time.monotonic() - t0,
                             compactions=0, prompt_tokens=0, completion_tokens=0,
                             cache_hit_tokens=0, stream=stream, error=str(e)[-2000:])
    finally:
        # A slow or failing teardown must not discard the result or stop the other instances;
        # a leftover container keeps its sciteam.run label and can be removed afterwards.
        for close in (sb.close, llm.aclose):
            try:
                await close()
            except Exception as e:
                print(f"[{iid}] teardown failed: {type(e).__name__}: {e}", flush=True)
    log(out)
    return out


async def image_present(image: str) -> bool:
    proc = await asyncio.create_subprocess_exec("docker", "image", "inspect", image,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


async def pull_image(image: str, attempts: int = 5) -> None:
    """Best effort; on failure the instance's sandbox start pulls again and reports the error."""
    for attempt in range(attempts):
        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", "--platform", "linux/amd64", "-q", image,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await proc.wait() == 0:
            return
        await asyncio.sleep(30 * (attempt + 1))


async def run_swebench(*, dataset: Path, ids: list[str] | None, harness_cfg: str, run_dir: Path,
                       configs_dir: Path, concurrency: int, model: str | None = None,
                       pull_concurrency: int = 2, min_free_gb: float = 50) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    configs = ConfigStore(configs_dir)
    cfg = configs.load(harness_cfg, HarnessConfig)
    if model:
        cfg.model = model
    store = EventStore(run_dir / "events.db")
    blobs = BlobStore(run_dir / "blobs")
    results_path = run_dir / "results.jsonl"
    done = {iid for iid, r in latest_results(results_path).items() if is_valid(r)}
    instances = [i for i in load_instances(dataset, ids) if i["instance_id"] not in done]
    (run_dir / "config.json").write_text(json.dumps({
        "harness": cfg.model_dump(mode="json"), "snapshot": configs.snapshot().model_dump(),
        "dataset": str(dataset), "n": len(instances) + len(done)}, indent=2))
    sem = asyncio.Semaphore(concurrency)
    # Pulls are bandwidth-bound: they get their own small queue and never hold an agent slot.
    pull_sem = asyncio.Semaphore(pull_concurrency)
    present = await asyncio.gather(*(image_present(image_for(i["instance_id"])) for i in instances))
    instances = [i for i, p in zip(instances, present, strict=True) if p] + \
        [i for i, p in zip(instances, present, strict=True) if not p]

    halted = asyncio.Event()

    def log(r: InstanceResult) -> None:
        with results_path.open("a") as f:
            f.write(r.model_dump_json() + "\n")
        print(f"[{time.strftime('%H:%M:%S')}] {r.instance_id}: {r.outcome}"
              f"{'/' + r.reason if r.reason else ''} steps={r.steps} ${r.cost_usd:.2f} "
              f"{r.wall_s / 60:.1f}min patch={r.patch_chars}", flush=True)
        if r.reason in HALT_REASONS and not halted.is_set():
            halted.set()
            print(f"HALT: {r.reason}; no new instances will start. Fix the provider account "
                  f"and rerun the same command to resume.", flush=True)

    def disk_ok() -> bool:
        low = [p for p in (run_dir, Path("/var/lib/docker")) if p.exists() and
               shutil.disk_usage(p).free < min_free_gb * 2**30]
        if low and not halted.is_set():
            halted.set()
            print(f"HALT: less than {min_free_gb} GB free on {low[0]}; no new instances will "
                  f"start. Free disk space and rerun the same command to resume.", flush=True)
        return not low

    async def one(inst):
        image = image_for(inst["instance_id"])
        if not await image_present(image):
            async with pull_sem:
                if halted.is_set() or not disk_ok():
                    return None
                await pull_image(image)
        async with sem:
            if halted.is_set() or not disk_ok():
                return None
            return await run_instance(inst, cfg=cfg, configs=configs,
                                      models_path=configs_dir / "models.yaml", run_dir=run_dir,
                                      store=store, blobs=blobs, log=log)

    await asyncio.gather(*(one(i) for i in instances))
    write_predictions(run_dir, cfg.name + "@" + cfg.model)


# Outcomes caused by the provider account or the driver, not by the agent: rerun on resume.
INFRA_REASONS = {"provider_quota", "provider_unavailable", "auth"}


def latest_results(path: Path) -> dict[str, dict[str, Any]]:
    """Last record per instance wins; results.jsonl is append-only, so reclassification appends."""
    out: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["instance_id"]] = r
    return out


def is_valid(r: dict[str, Any]) -> bool:
    return r["outcome"] != "driver_error" and r.get("reason") not in INFRA_REASONS


def write_predictions(run_dir: Path, model_name: str) -> Path:
    """Only instances whose latest result is a valid agent outcome are submitted for scoring."""
    valid = {iid for iid, r in latest_results(run_dir / "results.jsonl").items() if is_valid(r)}
    preds = run_dir / "preds.jsonl"
    with preds.open("w") as f:
        for p in sorted((run_dir / "patches").glob("*.diff")):
            if p.stem in valid:
                f.write(json.dumps({"instance_id": p.stem, "model_name_or_path": model_name,
                                    "model_patch": p.read_text()}) + "\n")
    return preds
