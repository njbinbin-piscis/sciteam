"""Metrics of the round protocol (docs/70): macro average, promote/reject, forgetting, negative
transfer, erroneous promotion, cost per gain, with an interval over source orders."""

from __future__ import annotations

import random
from statistics import mean
from typing import Any

from sciteam.experiment.rounds import ProtocolResult

MIN_ORDERS_FOR_INTERVAL = 3


def macro(d: dict[str, float]) -> float:
    return mean(d.values()) if d else 0.0


def run_summary(r: ProtocolResult) -> dict[str, Any]:
    promoted = [e for e in r.ledger if e["decision"] == "promoted"]
    rejected = [e for e in r.ledger if e["decision"] == "rejected"]
    # forgetting: how far the replay tasks fell below their first score, averaged
    drops = [max(0.0, r.replay_before[t] - r.replay_after[t])
             for t in r.replay_after if t in r.replay_before]
    # negative transfer: held-out tasks that ended worse than they started
    held = list(r.heldout_first)
    neg = [t for t in held if r.heldout_last[t] < r.heldout_first[t]]
    gain = macro(r.heldout_last) - macro(r.heldout_first)
    # promotions are erroneous when something was promoted yet held-out macro fell
    wrong = len(promoted) if promoted and gain < 0 else 0
    return {
        "arm": r.arm, "order_seed": r.order_seed,
        "heldout_macro": macro(r.heldout_last), "heldout_macro_start": macro(r.heldout_first),
        "ood_macro": macro(r.ood_last), "ood_macro_start": macro(r.ood_first),
        "promoted": len(promoted), "rejected": len(rejected),
        "forgetting": mean(drops) if drops else 0.0,
        "negative_transfer": len(neg) / len(held) if held else 0.0,
        "erroneous_promotions": wrong,
        "gain": gain,
        "cost": r.cost,
        "cost_per_gain": r.cost / gain if gain > 0 else None,
    }


def interval(xs: list[float], seed: int = 0, n: int = 2000) -> tuple[float, float] | None:
    """Percentile bootstrap of the mean; None below the minimum number of orders."""
    if len(xs) < MIN_ORDERS_FOR_INTERVAL:
        return None
    rng = random.Random(seed)
    means = sorted(mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def arm_table(results: list[ProtocolResult]) -> dict[str, dict[str, Any]]:
    """One row per arm; every metric is the mean over source orders plus its interval."""
    by: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by.setdefault(r.arm, []).append(run_summary(r))
    out: dict[str, dict[str, Any]] = {}
    for arm, rows in by.items():
        row: dict[str, Any] = {"orders": len(rows)}
        for key in ("heldout_macro", "ood_macro", "promoted", "rejected", "forgetting",
                    "negative_transfer", "erroneous_promotions", "gain", "cost"):
            xs = [float(x[key]) for x in rows]
            row[key] = {"mean": mean(xs), "ci": interval(xs)}
        cg = [x["cost_per_gain"] for x in rows if x["cost_per_gain"] is not None]
        row["cost_per_gain"] = mean(cg) if cg else None
        out[arm] = row
    return out
