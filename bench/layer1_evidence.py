"""H6/H7 evidence on a real model (docs/91-roadmap.md §9.6).

A child process runs one activation, commits the first tool result, then exits without
closing the activation. The parent resumes that stream (H6) while recording provider
traffic, then resumes a snapshot of the crashed state through ReplayLlm (H7).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sciteam.harness import Harness, HarnessConfig  # noqa: E402
from sciteam.kernel import BlobStore, EventStore  # noqa: E402
from sciteam.providers.llm import load_models, make_provider  # noqa: E402
from sciteam.providers.llm.fakes import RecordingLlm, ReplayLlm  # noqa: E402
from sciteam.providers.sandbox.local import LocalSandbox  # noqa: E402

STREAM = "layer1/h6"
TASK = (
    "Using bash, write the single line hello into the file note.txt in the working directory. "
    "Then call finish with outcome completed."
)


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _cfg() -> HarnessConfig:
    cfg = HarnessConfig(name="layer1-evidence", model="deepseek-flash", reasoning_effort="low",
                        system_prompt_ref="prompts/coder.md")
    cfg.budget.max_steps = 6
    cfg.budget.cost_usd = 1.0
    cfg.budget.wall_clock_s = 600
    cfg.compaction.enabled = False
    return cfg


def _harness(root: Path, llm, store: EventStore, blobs: BlobStore) -> Harness:
    sb = LocalSandbox(root / "work")
    info = load_models(ROOT / "configs/models.yaml")["deepseek-flash"]
    return Harness(_cfg(), model_info=info, llm=llm, sandbox=sb, store=store, blobs=blobs,
                   system_prompt="You are a test agent. Follow the task literally.")


async def _crash(root: Path) -> None:
    (root / "work").mkdir(parents=True)
    store, blobs = EventStore(root / "events.db"), BlobStore(root / "blobs")
    info = load_models(ROOT / "configs/models.yaml")["deepseek-flash"]
    llm = make_provider(info)
    h = _harness(root, llm, store, blobs)

    def stop(typ: str, _payload: dict) -> None:
        if typ == "tool_finished":
            store.close()
            os._exit(99)

    h.on_event = stop
    await sb_start(h)
    try:
        await h.run(TASK, stream=STREAM)
    finally:
        await h.sandbox.close()
        await llm.aclose()
    raise SystemExit("model finished before any tool call")


async def sb_start(h: Harness) -> None:
    await h.sandbox.start()


def _copy_snapshot(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copytree(src / "work", dest / "work")
    shutil.copytree(src / "blobs", dest / "blobs")
    for name in ("events.db", "events.db-wal", "events.db-shm"):
        f = src / name
        if f.exists():
            shutil.copy(f, dest / name)


async def _resume(root: Path, llm) -> tuple[str, list[tuple]]:
    store, blobs = EventStore(root / "events.db"), BlobStore(root / "blobs")
    h = _harness(root, llm, store, blobs)
    await h.sandbox.start()
    try:
        result = await h.resume(STREAM)
    finally:
        await h.sandbox.close()
        if hasattr(llm, "aclose"):
            await llm.aclose()
        elif hasattr(llm, "inner"):
            await llm.inner.aclose()
        store.close()
    evs = [(e.type, e.payload) for e in EventStore(root / "events.db").read(STREAM)]
    return result.outcome, evs


def _strip(evs: list[tuple]) -> list[tuple]:
    drop = {"duration_s", "wall_s", "ts"}

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items() if k not in drop}
        return x

    return [(t, walk(p)) for t, p in evs]


async def main() -> None:
    _load_dotenv()
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    if "--crash" in sys.argv:
        await _crash(root)
        return
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), str(root), "--crash")
    code = await proc.wait()
    if code != 99:
        raise SystemExit(f"crash child exited {code}, expected 99")
    crashed = EventStore(root / "events.db")
    evs = crashed.read(STREAM)
    crashed.close()
    if not evs or evs[-1].type == "activation_finished":
        raise SystemExit(f"crash did not leave an open activation (tail={evs[-1].type if evs else None})")
    snap = root / "snapshot"
    _copy_snapshot(root, snap)
    info = load_models(ROOT / "configs/models.yaml")["deepseek-flash"]
    rec_blobs = BlobStore(root / "recording")
    index: list[dict] = []
    recorded = RecordingLlm(make_provider(info), rec_blobs, index)
    outcome, resumed = await _resume(root, recorded)
    (root / "recording_index.json").write_text(json.dumps(index))
    replayed_outcome, replayed = await _resume(snap, ReplayLlm(rec_blobs, index, model_info=info))
    prefix = len(evs)
    same = _strip(resumed[prefix:]) == _strip(replayed[prefix:])
    summary = {
        "crash_exit": code,
        "crash_tail": evs[-1].type,
        "crash_events": len(evs),
        "resume_outcome": outcome,
        "replay_outcome": replayed_outcome,
        "recorded_calls": len(index),
        "suffix_equal": same,
        "resume_suffix": [t for t, _ in resumed[prefix:]],
        "replay_suffix": [t for t, _ in replayed[prefix:]],
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    if not same or outcome != replayed_outcome:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
