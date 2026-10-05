"""Experiment report (docs/60 §3.4): per-arm summary, paired differences against the first
arm with a task-level bootstrap CI, and the runs left out with their reasons."""

import json
import random
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from sciteam.experiment.runner import RESULT

BOOTSTRAP = 2000


def load_results(out: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(out.glob(f"*/*/r*/{RESULT}"))]


def exclusion(r: dict[str, Any]) -> str | None:
    if not r.get("audit_ok"):
        return "audit missing"
    if not r.get("replay_ok"):
        return "replay mismatch"
    if r.get("breaker"):
        return f"breaker ({r.get('end_reason')})"
    if not r.get("graded"):
        return "not graded"
    return None


def _per_task(x: float | dict[str, float] | None, task: str) -> float | None:
    return x.get(task) if isinstance(x, dict) else x


def closure_metrics(r: dict[str, Any], theta, success) -> dict[str, bool | None]:
    """false_closure: closed below the single-agent baseline θ (the registered v0.2 metric).
    The split (docs/60 §5): institutional_false_closure / true_closure judge the closure
    against the task's own success predicate; below_single compares with θ, closed or not."""
    score, closed = r.get("score"), r.get("closed")
    th, ok = _per_task(theta, r["task"]), _per_task(success, r["task"])
    known = score is not None and closed is not None
    return {
        "false_closure": bool(closed) and score < th if known and th is not None else None,
        "institutional_false_closure":
            bool(closed) and score < ok if known and ok is not None else None,
        "true_closure": bool(closed) and score >= ok if known and ok is not None else None,
        "below_single": score < th if score is not None and th is not None else None,
    }


def _ci(diffs: list[float], seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    boots = sorted(mean(rng.choice(diffs) for _ in diffs) for _ in range(BOOTSTRAP))
    return boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]


def hierarchical_ci(base: dict[str, list[float]], other: dict[str, list[float]],
                    seed: int = 0) -> tuple[float, float]:
    """Tasks are clusters and repeats nest in them: resample tasks, then each arm's repeats
    within every drawn task, and take the mean paired difference."""
    rng = random.Random(seed)
    tasks = sorted(set(base) & set(other))
    boots = []
    for _ in range(BOOTSTRAP):
        drawn = [rng.choice(tasks) for _ in tasks]
        boots.append(mean(mean(rng.choice(other[t]) for _ in other[t])
                          - mean(rng.choice(base[t]) for _ in base[t]) for t in drawn))
    boots.sort()
    return boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]


def identity_drift(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identity components (grader, environment, seed, …) that differ between runs of the
    same task: arms compared on that task did not face the same conditions."""
    seen: dict[tuple[str, str], set[str]] = {}
    for r in runs:
        for k, v in (r.get("identity") or {}).items():
            seen.setdefault((r["task"], k), set()).add(v)
        seen.setdefault((r["task"], "models_seen"), set()).add(",".join(r.get("models_seen", [])))
    return [{"task": t, "component": k, "values": len(v)}
            for (t, k), v in sorted(seen.items()) if len(v) > 1]


def summarize(out: Path) -> dict[str, Any]:
    spec = yaml.safe_load((out / "spec.yaml").read_text())
    theta, success, primary = spec.get("theta"), spec.get("success"), spec["primary"]
    arms = [a["name"] for a in spec["arms"]]
    results = load_results(out)
    digests = sorted({r["interpreter_digest"] for r in results})
    kept, excluded = [], []
    for r in results:
        why = exclusion(r)
        if why:
            excluded.append({"arm": r["arm"], "task": r["task"], "r": r["r"], "why": why})
        else:
            kept.append({**r, **closure_metrics(r, theta, success)})

    def val(r: dict[str, Any], name: str) -> float | None:
        v = r.get(name)
        return None if v is None else float(v)

    per_arm: dict[str, Any] = {}
    for arm in arms:
        rs = [r for r in kept if r["arm"] == arm]
        row: dict[str, Any] = {"runs": len(rs)}
        for name in ("score", "closed", "false_closure", "institutional_false_closure",
                     "true_closure", "below_single", "stuck", "recovered", "cost_usd",
                     "wall_s", "messages", "amendments"):
            vs = [v for v in (val(r, name) for r in rs) if v is not None]
            row[name] = round(mean(vs), 4) if vs else None
        per_arm[arm] = row

    def task_values(arm: str) -> dict[str, list[float]]:
        by: dict[str, list[float]] = {}
        for r in kept:
            v = val(r, primary)
            if r["arm"] == arm and v is not None:
                by.setdefault(r["task"], []).append(v)
        return by

    paired: dict[str, Any] = {}
    base = task_values(arms[0])
    for arm in arms[1:]:
        other = task_values(arm)
        common = sorted(set(base) & set(other))
        diffs = [mean(other[t]) - mean(base[t]) for t in common]
        paired[arm] = {"vs": arms[0], "tasks": len(common),
                       "mean_diff": round(mean(diffs), 4) if diffs else None,
                       "per_task": {t: round(d, 4) for t, d in zip(common, diffs, strict=True)},
                       "ci95": [round(x, 4) for x in hierarchical_ci(base, other)]
                       if len(common) > 1 else None}
    return {"name": spec["name"], "primary": primary, "theta": theta, "success": success,
            "valid": len(digests) <= 1, "interpreter_digests": digests,
            "identity_drift": identity_drift(kept),
            "arms": per_arm, "paired": paired, "excluded": excluded}


def render_report(s: dict[str, Any]) -> str:
    lines = [f"experiment {s['name']}: primary={s['primary']} theta={s['theta']}",
             "VALID" if s["valid"] else
             f"INVALID: {len(s['interpreter_digests'])} interpreter digests in one experiment",
             f"interpreter {', '.join(d[:12] for d in s['interpreter_digests'])}", ""]
    cols = ["runs", "score", "closed", "false_closure", "institutional_false_closure",
            "true_closure", "below_single", "stuck", "recovered", "cost_usd", "wall_s"]
    lines.append("| arm | " + " | ".join(cols) + " |")
    lines.append("|---" * (len(cols) + 1) + "|")
    for arm, row in s["arms"].items():
        lines.append(f"| {arm} | " + " | ".join("-" if row[c] is None else str(row[c])
                                               for c in cols) + " |")
    lines.append("")
    for arm, p in s["paired"].items():
        lines.append(f"{arm} − {p['vs']} ({s['primary']}, {p['tasks']} tasks): "
                     f"{p['mean_diff']} CI95 {p['ci95']}")
    if s["identity_drift"]:
        lines += ["", "identity drift (same task, different conditions):"] + [
            f"- {d['task']}: {d['component']} has {d['values']} values"
            for d in s["identity_drift"]]
    if s["excluded"]:
        lines += ["", "excluded:"] + [f"- {e['arm']}/{e['task']}/r{e['r']}: {e['why']}"
                                      for e in s["excluded"]]
    return "\n".join(lines)
