"""M0 Statistical Power, Variance Decomposition & Paired Bootstrap Analyzer.

Computes per-task paired differences Delta_i = mean(O_k, task_i) - mean(base, task_i),
10,000-sample paired bootstrap 95% confidence intervals, within-task variance (sigma^2_within),
between-task variance of paired differences (sigma^2_between), required minimum paired task
sample size N_min for (alpha=0.05, power=0.80), and evaluates Kill Criterion 0.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from sciteam.experiment.report import exclusion, load_results

BOOTSTRAP_SAMPLES = 10_000
Z_ALPHA_TWO_SIDED_005 = 1.959963984540054
Z_BETA_POWER_080 = 0.8416212335729143


def _sample_variance(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def paired_bootstrap_ci(
    diffs: list[float],
    *,
    n_boot: int = BOOTSTRAP_SAMPLES,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Return (ci_low, ci_high, two_sided_p_value) from paired task-level differences."""
    if not diffs:
        return 0.0, 0.0, 1.0
    rng = random.Random(seed)
    n = len(diffs)
    boots = sorted(mean(rng.choice(diffs) for _ in range(n)) for _ in range(n_boot))
    lo_idx = max(0, int((alpha / 2.0) * n_boot))
    hi_idx = min(n_boot - 1, int((1.0 - alpha / 2.0) * n_boot) - 1)
    # Empirical two-sided p-value against H0: mean_diff == 0
    le_zero = sum(1 for b in boots if b <= 0.0) / n_boot
    ge_zero = sum(1 for b in boots if b >= 0.0) / n_boot
    p_val = min(1.0, 2.0 * min(le_zero, ge_zero))
    return boots[lo_idx], boots[hi_idx], p_val


def required_sample_size(
    sigma2_between: float,
    sigma2_within: float,
    repeats: int = 1,
    target_delta: float = 0.05,
    *,
    z_alpha: float = Z_ALPHA_TWO_SIDED_005,
    z_beta: float = Z_BETA_POWER_080,
) -> int:
    """Compute minimum paired tasks N_min to detect target_delta at (alpha, power).

    For paired task-level differences averaged over R repeats per arm:
      Var(Delta_i) = sigma^2_between + 2 * sigma^2_within / R
      N_min = ceil(((z_{alpha/2} + z_beta)^2 * Var(Delta_i)) / target_delta^2)
    """
    if target_delta <= 0:
        raise ValueError("target_delta must be positive")
    r = max(1, repeats)
    eff_var = max(0.0, sigma2_between) + (2.0 * max(0.0, sigma2_within) / r)
    if eff_var == 0.0:
        return 1
    raw_n = ((z_alpha + z_beta) ** 2 * eff_var) / (target_delta**2)
    return max(1, math.ceil(raw_n))


def analyze_arm_pair(
    base_by_task: dict[str, list[float]],
    cand_by_task: dict[str, list[float]],
    *,
    target_delta: float = 0.05,
    n_boot: int = BOOTSTRAP_SAMPLES,
    seed: int = 42,
) -> dict[str, Any]:
    """Compute paired statistics between baseline and one candidate arm."""
    common_tasks = sorted(set(base_by_task) & set(cand_by_task))
    paired_diffs: dict[str, float] = {}
    within_vars: list[float] = []
    repeats_observed: list[int] = []

    for t in common_tasks:
        b_scores = base_by_task[t]
        c_scores = cand_by_task[t]
        if not b_scores or not c_scores:
            continue
        paired_diffs[t] = mean(c_scores) - mean(b_scores)
        if len(b_scores) >= 2:
            within_vars.append(_sample_variance(b_scores))
        if len(c_scores) >= 2:
            within_vars.append(_sample_variance(c_scores))
        repeats_observed.append(min(len(b_scores), len(c_scores)))

    diff_list = [paired_diffs[t] for t in sorted(paired_diffs)]
    mean_delta = mean(diff_list) if diff_list else 0.0
    sigma2_within = mean(within_vars) if within_vars else 0.0
    total_diff_var = _sample_variance(diff_list) if len(diff_list) >= 2 else 0.0
    avg_r = round(mean(repeats_observed)) if repeats_observed else 1
    # Decompose observed variance of task-level difference into between-task and within-task
    sigma2_between = max(0.0, total_diff_var - (2.0 * sigma2_within / max(1, avg_r)))
    ci_low, ci_high, p_value = paired_bootstrap_ci(diff_list, n_boot=n_boot, seed=seed)
    n_min = required_sample_size(
        sigma2_between=sigma2_between,
        sigma2_within=sigma2_within,
        repeats=max(1, avg_r),
        target_delta=target_delta,
    )

    return {
        "n_paired_tasks": len(diff_list),
        "avg_repeats_per_task": avg_r,
        "mean_delta": round(mean_delta, 6),
        "ci_95": [round(ci_low, 6), round(ci_high, 6)],
        "p_value": round(p_value, 6),
        "sigma2_within": round(sigma2_within, 6),
        "sigma2_between": round(sigma2_between, 6),
        "total_paired_diff_var": round(total_diff_var, 6),
        "required_n_min": n_min,
        "per_task_delta": {k: round(v, 6) for k, v in paired_diffs.items()},
    }


def analyze_experiment(
    out_dir: Path,
    *,
    baseline_arm: str | None = None,
    target_delta: float = 0.05,
    kill_threshold: float = 0.03,
    n_boot: int = BOOTSTRAP_SAMPLES,
    seed: int = 42,
) -> dict[str, Any]:
    """Analyze an experiment output directory and evaluate Kill Criterion 0."""
    spec_file = out_dir / "spec.yaml"
    spec = yaml.safe_load(spec_file.read_text()) if spec_file.exists() else {}
    primary = spec.get("primary", "score")
    arms = [a["name"] for a in spec.get("arms", [])]
    results = load_results(out_dir)

    by_arm_task: dict[str, dict[str, list[float]]] = {}
    for r in results:
        if exclusion(r) is not None:
            continue
        val = r.get(primary)
        if val is None:
            continue
        by_arm_task.setdefault(r["arm"], {}).setdefault(r["task"], []).append(float(val))

    if not arms:
        arms = sorted(by_arm_task.keys())
    base_name = baseline_arm or (arms[0] if arms else "baseline")
    base_scores = by_arm_task.get(base_name, {})

    comparisons: dict[str, dict[str, Any]] = {}
    max_delta = -math.inf
    any_ci_excludes_zero = False
    recommended_n_min = 1

    for arm in arms:
        if arm == base_name:
            continue
        comp = analyze_arm_pair(
            base_scores,
            by_arm_task.get(arm, {}),
            target_delta=target_delta,
            n_boot=n_boot,
            seed=seed,
        )
        comparisons[arm] = comp
        if comp["n_paired_tasks"] > 0:
            max_delta = max(max_delta, comp["mean_delta"])
            ci_lo, ci_hi = comp["ci_95"]
            if ci_lo > 0.0 or ci_hi < 0.0:
                any_ci_excludes_zero = True
            recommended_n_min = max(recommended_n_min, comp["required_n_min"])

    if max_delta == -math.inf:
        max_delta = 0.0

    # Kill Criterion 0: max_delta < +0.03 AND 95% paired Bootstrap CI covers 0
    kill_criterion_0_triggered = (max_delta < kill_threshold) and (not any_ci_excludes_zero)

    return {
        "experiment": spec.get("name", out_dir.name),
        "baseline_arm": base_name,
        "primary_metric": primary,
        "target_delta_sesoi": target_delta,
        "kill_criterion_0_threshold": kill_threshold,
        "max_mean_delta": round(max_delta, 6),
        "kill_criterion_0_triggered": kill_criterion_0_triggered,
        "recommended_n_min": recommended_n_min,
        "comparisons": comparisons,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Compute M0 statistical power and paired bootstrap CI")
    ap.add_argument("out_dir", type=Path, help="Experiment output directory")
    ap.add_argument("--baseline", help="Baseline arm name (defaults to first arm in spec)")
    ap.add_argument("--sesoi", type=float, default=0.05, help="Target effect size (SESOI)")
    ap.add_argument("--kill-threshold", type=float, default=0.03, help="Kill Criterion 0 threshold")
    ap.add_argument("--boot", type=int, default=BOOTSTRAP_SAMPLES, help="Bootstrap iterations")
    ap.add_argument("--json", dest="json_out", type=Path, help="Write JSON report to path")
    ap.add_argument("--update-gate-policy", type=Path, help="Update min_paired_tasks_n in gate-policy.yaml")
    args = ap.parse_args(argv)

    report = analyze_experiment(
        args.out_dir,
        baseline_arm=args.baseline,
        target_delta=args.sesoi,
        kill_threshold=args.kill_threshold,
        n_boot=args.boot,
    )
    text = json.dumps(report, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n")
    if args.update_gate_policy and args.update_gate_policy.exists():
        policy = yaml.safe_load(args.update_gate_policy.read_text()) or {}
        policy["min_paired_tasks_n"] = report["recommended_n_min"]
        policy["kill_criterion_0_triggered"] = report["kill_criterion_0_triggered"]
        args.update_gate_policy.write_text(yaml.safe_dump(policy, sort_keys=False))
    print(text)


if __name__ == "__main__":
    main()
