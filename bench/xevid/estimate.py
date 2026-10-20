"""Pre-registration cost and wall-time estimate for X-evid (docs/60-experiment.md §6.6), from the
single-agent screening runs on the same problems. Lower bound: one implementation per member,
one validation per run, no reconciliation rounds.

With `--smoke DIR` it also projects from the one-problem smoke runs (n=1 per arm, so only an
order of magnitude: the smoke includes reconciliation rounds, the lower bound does not).

Usage: python bench/xevid/estimate.py [--smoke runs/exp/x-evid-smoke]
"""

import argparse
import json
import statistics as st
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "configs/experiments/x-evid.yaml"
SCREEN = {"flash": "x-base-scicode", "mimo": "x-base-scicode-mimo"}
ARMS = {
    "replicate-same": ("flash", "flash"),
    "replicate-cross": ("flash", "mimo"),
    "compute-control": ("flash", "flash"),
}
VALIDATE_S = (480, 700)  # p13 self-pair in the sandbox and a disagreeing pair on the host


def screening(exp: str, only: set[str]) -> dict:
    cost, wall = [], []
    for f in (ROOT / "runs/exp" / exp).glob("*/*/r*/result.json"):
        d = json.loads(f.read_text())
        if d["task"] in only and d.get("cost_usd") is not None:
            cost.append(d["cost_usd"])
            wall.append(d["wall_s"])
    return {"n": len(cost), "cost": st.mean(cost), "wall": st.mean(wall)}


def project_from_smoke(smoke: Path, runs: int, parallel: int) -> None:
    total, hours = 0.0, 0.0
    for arm in ARMS:
        f = next(smoke.glob(f"{arm}/*/r*/result.json"), None)
        if f is None:
            return
        d = json.loads(f.read_text())
        total += runs * d["cost_usd"]
        hours += runs * d["wall_s"] / 3600
        print(f"smoke {arm:16s} cost=${d['cost_usd']:.2f} wall={d['wall_s'] / 60:.0f}min")
    print(f"smoke projection: cost ~${total:.0f}; serial ~{hours:.0f}h; "
          f"parallel {parallel} ~{hours / parallel:.0f}h ({hours / parallel / 24:.1f} days)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=Path)
    ap.add_argument("--parallel", type=int)
    args = ap.parse_args()
    spec = yaml.safe_load(SPEC.read_text())
    only = set(spec["only_tasks"])
    runs = len(only) * spec["repeats"]
    m = {k: screening(v, only) for k, v in SCREEN.items()}
    total, hours = 0.0, 0.0
    for arm, (a, b) in ARMS.items():
        cost = runs * (m[a]["cost"] + m[b]["cost"])
        validate = VALIDATE_S if arm != "compute-control" else (0, 0)
        wall = max(m[a]["wall"], m[b]["wall"]) + validate[1]
        total += cost
        hours += runs * wall / 3600
        print(f"{arm:16s} runs={runs} cost>=${cost:.0f} wall/run>={wall / 60:.0f}min")
    print(
        "screening: " + ", ".join(f"{k} n={v['n']} ${v['cost']:.3f} {v['wall']:.0f}s" for k, v in m.items())
    )
    print(
        f"total cost>=${total:.0f}; serial>={hours:.0f}h; "
        f"parallel {spec['parallel']}>={hours / spec['parallel']:.0f}h"
    )
    if args.smoke:
        project_from_smoke(args.smoke, runs, args.parallel or spec["parallel"])


if __name__ == "__main__":
    main()
