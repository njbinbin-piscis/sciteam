"""X-evid analysis (docs/60-experiment.md §6.6): every reported number comes from here.

Per arm: closed rate, mean score (0 when not closed), precision = P(score >= success | closed),
false closures, coverage loss (not closed although implementer 1 was fully correct), and the
paired differences against `compute-control` with a task-clustered bootstrap CI. Runs whose
audit failed (replay_ok false), or that ended on a provider breaker, are excluded and counted.

Usage: python bench/xevid/evid_analyze.py runs/exp/x-evid [--json out.json] [--boot 5000]
"""

import argparse
import json
import random
import statistics as st
from collections import defaultdict
from pathlib import Path

SUCCESS = 1.0
CONTROL = "compute-control"


def load(exp: Path) -> tuple[dict, dict]:
    runs: dict[str, list[dict]] = defaultdict(list)
    excluded: dict[str, int] = defaultdict(int)
    for f in sorted(exp.glob("*/*/r*/result.json")):
        res = json.loads(f.read_text())
        sc = f.with_name("score.json")
        if not sc.exists() or res.get("status") == "paused":
            excluded[res["arm"]] += 1
            continue
        if res.get("replay_ok") is False or res.get("breaker"):
            excluded[res["arm"]] += 1
            continue
        g = json.loads(sc.read_text())
        runs[res["arm"]].append(
            {
                "task": res["task"],
                "closed": bool(g["closed"]),
                "score": float(g["score"]),
                "impl1_full": float(g["impl1"]["score"]) >= SUCCESS,
                "impl2_full": float((g.get("impl2") or {}).get("score", 0.0)) >= SUCCESS,
                "returned": int(res.get("returned") or 0),
                "cost": res.get("cost_usd") or 0.0,
            }
        )
    return runs, excluded


def arm_stats(rs: list[dict]) -> dict:
    closed = [r for r in rs if r["closed"]]
    full = [r for r in closed if r["score"] >= SUCCESS]
    r1_closed = [r for r in closed if r["returned"] == 0]
    r1_full = [r for r in r1_closed if r["score"] >= SUCCESS]
    r2_closed = [r for r in closed if r["returned"] > 0]
    r2_full = [r for r in r2_closed if r["score"] >= SUCCESS]
    matrix = {}
    for b1 in (True, False):
        for b2 in (True, False):
            sub = [r for r in rs if r["impl1_full"] == b1 and r["impl2_full"] == b2]
            sub_c = [r for r in sub if r["closed"]]
            matrix[f"{int(b1)}{int(b2)}"] = {
                "total": len(sub),
                "closed": len(sub_c),
                "closed_rate": len(sub_c) / len(sub) if sub else None,
            }
    return {
        "n": len(rs),
        "closed": len(closed),
        "closed_rate": len(closed) / len(rs) if rs else None,
        "score": st.mean(r["score"] for r in rs) if rs else None,
        "closed_score": st.mean(r["score"] for r in closed) if closed else None,
        "full_correct": len(full),
        "precision": len(full) / len(closed) if closed else None,
        "round1_closed": len(r1_closed),
        "round1_full_correct": len(r1_full),
        "precision_round1": len(r1_full) / len(r1_closed) if r1_closed else None,
        "round2_closed": len(r2_closed),
        "round2_full_correct": len(r2_full),
        "precision_round2": len(r2_full) / len(r2_closed) if r2_closed else None,
        "false_closures": len(closed) - len(full),
        "false_closure_rate": (len(closed) - len(full)) / len(rs) if rs else None,
        "coverage_loss": sum(1 for r in rs if not r["closed"] and r["impl1_full"]),
        "matrix_2x2": matrix,
        "cost": sum(r["cost"] for r in rs),
    }


def by_task(rs: list[dict]) -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        d[r["task"]].append(r)
    return d


def fast_metric(rs: list[dict], metric: str) -> float | None:
    if not rs:
        return None
    if metric == "score":
        return st.mean(r["score"] for r in rs)
    closed = [r for r in rs if r["closed"]]
    if metric == "closed_rate":
        return len(closed) / len(rs)
    if metric == "precision":
        return sum(1 for r in closed if r["score"] >= SUCCESS) / len(closed) if closed else None
    if metric == "precision_round1":
        r1 = [r for r in closed if r["returned"] == 0]
        return sum(1 for r in r1 if r["score"] >= SUCCESS) / len(r1) if r1 else None
    if metric == "false_closure_rate":
        return sum(1 for r in closed if r["score"] < SUCCESS) / len(rs)
    return arm_stats(rs)[metric]


def diff(
    ta: dict[str, list[dict]], tb: dict[str, list[dict]], metric: str, tasks: list[str]
) -> float | None:
    ra = [r for t in tasks for r in ta.get(t, [])]
    rb = [r for t in tasks for r in tb.get(t, [])]
    va, vb = fast_metric(ra, metric), fast_metric(rb, metric)
    return None if va is None or vb is None else va - vb


def bootstrap(a: list[dict], b: list[dict], metric: str, n: int, seed: int = 0):
    ta, tb = by_task(a), by_task(b)
    tasks = sorted(set(ta) & set(tb))
    point = diff(ta, tb, metric, tasks)
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        d = diff(ta, tb, metric, [rng.choice(tasks) for _ in tasks])
        if d is not None:
            vals.append(d)
    vals.sort()
    ci = [vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]] if vals else None
    return {"tasks": len(tasks), "diff": point, "ci95": ci}


def analyze(exp: Path, boot: int) -> dict:
    runs, excluded = load(exp)
    out = {
        "arms": {a: arm_stats(r) for a, r in runs.items()},
        "excluded": dict(excluded),
        "paired_vs_control": {},
    }
    if CONTROL in runs:
        for arm in runs:
            if arm != CONTROL:
                out["paired_vs_control"][arm] = {
                    m: bootstrap(runs[arm], runs[CONTROL], m, boot)
                    for m in (
                        "precision",
                        "precision_round1",
                        "false_closure_rate",
                        "score",
                        "closed_rate",
                    )
                }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exp", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--boot", type=int, default=5000)
    a = ap.parse_args()
    res = analyze(a.exp, a.boot)
    text = json.dumps(res, indent=2)
    if a.json:
        a.json.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
