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


def _false_closure(r: dict[str, Any], theta: float | dict[str, float] | None) -> bool | None:
    if isinstance(theta, dict):
        theta = theta.get(r["task"])
    if theta is None or r.get("score") is None:
        return None
    if r.get("closed") is None:
        return None
    return bool(r["closed"]) and r["score"] < theta


def _ci(diffs: list[float], seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    boots = sorted(mean(rng.choice(diffs) for _ in diffs) for _ in range(BOOTSTRAP))
    return boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]


def summarize(out: Path) -> dict[str, Any]:
    spec = yaml.safe_load((out / "spec.yaml").read_text())
    theta, primary = spec.get("theta"), spec["primary"]
    arms = [a["name"] for a in spec["arms"]]
    results = load_results(out)
    digests = sorted({r["interpreter_digest"] for r in results})
    kept, excluded = [], []
    for r in results:
        why = exclusion(r)
        if why:
            excluded.append({"arm": r["arm"], "task": r["task"], "r": r["r"], "why": why})
        else:
            r = {**r, "false_closure": _false_closure(r, theta)}
            kept.append(r)

    def val(r: dict[str, Any], name: str) -> float | None:
        v = r.get(name)
        return None if v is None else float(v)

    per_arm: dict[str, Any] = {}
    for arm in arms:
        rs = [r for r in kept if r["arm"] == arm]
        row: dict[str, Any] = {"runs": len(rs)}
        for name in ("score", "closed", "false_closure", "stuck", "recovered", "cost_usd",
                     "wall_s", "messages", "amendments"):
            vs = [v for v in (val(r, name) for r in rs) if v is not None]
            row[name] = round(mean(vs), 4) if vs else None
        per_arm[arm] = row

    def task_means(arm: str) -> dict[str, float]:
        by: dict[str, list[float]] = {}
        for r in kept:
            v = val(r, primary)
            if r["arm"] == arm and v is not None:
                by.setdefault(r["task"], []).append(v)
        return {t: mean(v) for t, v in by.items()}

    paired: dict[str, Any] = {}
    base = task_means(arms[0])
    for arm in arms[1:]:
        other = task_means(arm)
        common = sorted(set(base) & set(other))
        diffs = [other[t] - base[t] for t in common]
        paired[arm] = {"vs": arms[0], "tasks": len(common),
                       "mean_diff": round(mean(diffs), 4) if diffs else None,
                       "ci95": [round(x, 4) for x in _ci(diffs)] if len(diffs) > 1 else None}
    return {"name": spec["name"], "primary": primary, "theta": theta,
            "valid": len(digests) <= 1, "interpreter_digests": digests,
            "arms": per_arm, "paired": paired, "excluded": excluded}


def render_report(s: dict[str, Any]) -> str:
    lines = [f"experiment {s['name']}: primary={s['primary']} theta={s['theta']}",
             "VALID" if s["valid"] else
             f"INVALID: {len(s['interpreter_digests'])} interpreter digests in one experiment",
             f"interpreter {', '.join(d[:12] for d in s['interpreter_digests'])}", ""]
    cols = ["runs", "score", "closed", "false_closure", "stuck", "recovered", "cost_usd",
            "wall_s"]
    lines.append("| arm | " + " | ".join(cols) + " |")
    lines.append("|---" * (len(cols) + 1) + "|")
    for arm, row in s["arms"].items():
        lines.append(f"| {arm} | " + " | ".join("-" if row[c] is None else str(row[c])
                                               for c in cols) + " |")
    lines.append("")
    for arm, p in s["paired"].items():
        lines.append(f"{arm} − {p['vs']} ({s['primary']}, {p['tasks']} tasks): "
                     f"{p['mean_diff']} CI95 {p['ci95']}")
    if s["excluded"]:
        lines += ["", "excluded:"] + [f"- {e['arm']}/{e['task']}/r{e['r']}: {e['why']}"
                                      for e in s["excluded"]]
    return "\n".join(lines)
