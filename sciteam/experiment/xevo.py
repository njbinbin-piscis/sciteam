"""Channel A (G2) sequential constitutional evolution runner & counterfactual replay driver.

Orchestrates sequential task runs T_1 .. T_K for:
  1. `frozen` arm: runs each task independently with the initial T_0 configuration.
  2. `evolving` arm: carries enacted amendments from T_k into T_{k+1} via `carry()`, maintaining
     a cumulative overlay directory and verifying the `CARRIED.json` SHA-256 hash chain.
  3. `replay` (counterfactual replay): injects the final overlay from T_K back into the early
     tasks T_1 .. T_m (default m=3) and computes Delta_replay = Score(T_{1..m} | Overlay_{T_K})
     - Score(T_{1..m} | Overlay_{T_0}). Marks status as `rejected_degraded` if Delta_replay < 0.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from statistics import mean
from typing import Any

from sciteam.experiment.carry import carry
from sciteam.kernel.canonical import sha256_hex
from sciteam.kernel.events import EventStore

TaskRunnerFn = Callable[[str, str, Path, list[Path]], dict[str, Any]]


def verify_carried_overlay(run_dir: Path, overlay_dir: Path) -> dict[str, Any]:
    """Verify that CARRIED.json in `overlay_dir` matches `run_dir`'s final rule_version_activated."""
    carried_file = overlay_dir / "CARRIED.json"
    if not carried_file.exists():
        raise ValueError(f"Missing CARRIED.json in {overlay_dir}")
    meta = json.loads(carried_file.read_text())
    store = EventStore(run_dir / "events.db")
    try:
        versions = store.read_global(types={"rule_version_activated"})
    finally:
        store.close()
    if not versions:
        raise ValueError(f"Run {run_dir} has no rule_version_activated event")
    final_payload = versions[-1].payload
    if meta.get("rule_version") != final_payload.get("version"):
        raise ValueError(
            f"rule_version mismatch: CARRIED.json has {meta.get('rule_version')}, "
            f"event store has {final_payload.get('version')}"
        )
    for rel_path, expected_sha in (meta.get("files") or {}).items():
        disk_file = overlay_dir / rel_path
        if not disk_file.exists():
            raise ValueError(f"Carried file missing on disk: {disk_file}")
        actual_sha = sha256_hex(disk_file.read_text())
        if actual_sha != expected_sha or actual_sha != final_payload["files"].get(rel_path):
            raise ValueError(f"SHA-256 mismatch for carried file {rel_path}")
    return meta


def carry_cumulative(
    run_dir: Path,
    prev_overlay: Path | None,
    dest_overlay: Path,
) -> tuple[Path, dict[str, Any]]:
    """Carry enacted amendments from `run_dir` on top of `prev_overlay` into `dest_overlay`."""
    dest_overlay.mkdir(parents=True, exist_ok=True)
    prev_files: dict[str, str] = {}
    prev_chain: list[dict[str, Any]] = []
    if prev_overlay and prev_overlay.exists():
        for item in prev_overlay.rglob("*"):
            if item.is_file() and item.name != "CARRIED.json":
                rel = item.relative_to(prev_overlay)
                target = dest_overlay / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                prev_files[str(rel)] = sha256_hex(target.read_text())
        prev_meta_file = prev_overlay / "CARRIED.json"
        if prev_meta_file.exists():
            prev_meta = json.loads(prev_meta_file.read_text())
            prev_chain = list(prev_meta.get("hash_chain", []))

    step_dir = dest_overlay.parent / f".step_{dest_overlay.name}"
    if step_dir.exists():
        shutil.rmtree(step_dir)
    step_dir.mkdir(parents=True, exist_ok=True)
    step_files = carry(run_dir, step_dir)
    step_meta = verify_carried_overlay(run_dir, step_dir)

    for rel_path in step_files:
        src = step_dir / rel_path
        target = dest_overlay / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    shutil.rmtree(step_dir, ignore_errors=True)

    merged_files = {**prev_files, **step_files}
    chain_entry = {
        "from_run": str(run_dir),
        "rule_version": step_meta["rule_version"],
        "step_files": step_files,
        "cumulative_files": merged_files,
    }
    full_meta = {
        "from": str(run_dir),
        "rule_version": step_meta["rule_version"],
        "files": merged_files,
        "amendments": step_meta.get("amendments", []),
        "hash_chain": [*prev_chain, chain_entry],
    }
    (dest_overlay / "CARRIED.json").write_text(json.dumps(full_meta, indent=2) + "\n")
    return dest_overlay, full_meta


def run_xevo_sequence(
    tasks: list[str],
    out_dir: Path,
    runner_fn: TaskRunnerFn,
    *,
    replay_prefix_count: int = 3,
) -> dict[str, Any]:
    """Run `frozen` arm, `evolving` arm with sequential carry, and counterfactual replay."""
    out_dir.mkdir(parents=True, exist_ok=True)
    frozen_results: dict[str, dict[str, Any]] = {}
    evolving_results: dict[str, dict[str, Any]] = {}
    carry_ledger: list[dict[str, Any]] = []

    # 1. Frozen arm (Overlay_T0 on all tasks)
    for tid in tasks:
        rd = out_dir / "frozen" / tid / "r1"
        rd.mkdir(parents=True, exist_ok=True)
        frozen_results[tid] = runner_fn("frozen", tid, rd, [])

    # 2. Evolving arm (sequential carry T_k -> T_{k+1})
    current_overlay: Path | None = None
    for idx, tid in enumerate(tasks, start=1):
        rd = out_dir / "evolving" / tid / "r1"
        rd.mkdir(parents=True, exist_ok=True)
        overlays = [current_overlay] if current_overlay is not None else []
        res = runner_fn("evolving", tid, rd, overlays)
        evolving_results[tid] = res

        next_ov = out_dir / "overlays" / f"step_{idx:02d}_{tid}"
        current_overlay, meta = carry_cumulative(rd, current_overlay, next_ov)
        carry_ledger.append(
            {
                "step": idx,
                "task": tid,
                "rule_version": meta["rule_version"],
                "files": meta["files"],
                "overlay_dir": str(current_overlay),
            }
        )

    # 3. Counterfactual Replay: evaluate final overlay (Overlay_{T_K}) on early tasks T_1 .. T_m
    replay_tasks = tasks[: min(len(tasks), max(1, replay_prefix_count))]
    replay_results: dict[str, dict[str, Any]] = {}
    final_overlays = [current_overlay] if current_overlay is not None else []
    for tid in replay_tasks:
        rd = out_dir / "replay" / tid / "r1"
        rd.mkdir(parents=True, exist_ok=True)
        replay_results[tid] = runner_fn("replay", tid, rd, final_overlays)

    t0_scores = [float(evolving_results[t]["score"]) for t in replay_tasks]
    tk_scores = [float(replay_results[t]["score"]) for t in replay_tasks]
    delta_replay = mean(tk_scores) - mean(t0_scores) if replay_tasks else 0.0
    status = "rejected_degraded" if delta_replay < 0.0 else "promoted"

    summary = {
        "tasks": tasks,
        "replay_tasks": replay_tasks,
        "frozen_mean_score": round(mean(float(frozen_results[t]["score"]) for t in tasks), 6),
        "evolving_mean_score": round(mean(float(evolving_results[t]["score"]) for t in tasks), 6),
        "replay_initial_mean_score": round(mean(t0_scores), 6),
        "replay_final_mean_score": round(mean(tk_scores), 6),
        "delta_replay": round(delta_replay, 6),
        "status": status,
        "carry_ledger": carry_ledger,
        "final_overlay": str(current_overlay) if current_overlay else None,
    }
    (out_dir / "xevo_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
