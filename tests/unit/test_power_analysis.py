"""Unit tests for M0 statistical power analysis and ceiling probe specs."""

import json
from pathlib import Path

from sciteam.experiment.power import (
    analyze_experiment,
    main,
    paired_bootstrap_ci,
    required_sample_size,
)
from sciteam.experiment.spec import load_spec
from sciteam.kernel.config_store import ConfigStore

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"


def test_paired_bootstrap_and_sample_size_calculation():
    diffs = [0.10, 0.12, 0.08, 0.11, 0.09, 0.10, 0.13, 0.07]
    lo, hi, p = paired_bootstrap_ci(diffs, n_boot=2000, seed=7)
    assert lo > 0.07
    assert hi < 0.13
    assert p < 0.01

    n_min = required_sample_size(sigma2_between=0.01, sigma2_within=0.005, repeats=2, target_delta=0.05)
    assert n_min >= 40


def test_analyze_experiment_kill_criterion_0_and_cli(tmp_path):
    # Case 1: Strong improvement (+0.08) -> Kill Criterion 0 is False
    out_good = tmp_path / "m0-good"
    out_good.mkdir()
    (out_good / "spec.yaml").write_text(
        "name: m0-good\nprimary: score\narms:\n- {name: baseline}\n- {name: o3}\n"
    )
    for idx in range(1, 9):
        tid = f"p{idx}"
        for r in (1, 2):
            for arm, sc in (("baseline", 0.50), ("o3", 0.60)):
                rd = out_good / arm / tid / f"r{r}"
                rd.mkdir(parents=True)
                (rd / "result.json").write_text(
                    json.dumps(
                        {
                            "arm": arm,
                            "task": tid,
                            "r": r,
                            "audit_ok": True,
                            "replay_ok": True,
                            "breaker": False,
                            "graded": True,
                            "score": sc + (0.01 if r == 2 else -0.01),
                        }
                    )
                )
    json_out = out_good / "power.json"
    main([str(out_good), "--boot", "1000", "--json", str(json_out)])
    rep_good = json.loads(json_out.read_text())
    assert rep_good["kill_criterion_0_triggered"] is False
    assert abs(rep_good["max_mean_delta"] - 0.10) < 1e-5
    assert rep_good["comparisons"]["o3"]["ci_95"][0] > 0.0

    # Case 2: Negligible improvement (+0.005, CI covers 0) -> Kill Criterion 0 is True
    out_flat = tmp_path / "m0-flat"
    out_flat.mkdir()
    (out_flat / "spec.yaml").write_text(
        "name: m0-flat\nprimary: score\narms:\n- {name: baseline}\n- {name: o1}\n"
    )
    deltas = [0.02, -0.01, 0.01, -0.01, 0.02, -0.01]
    for idx, d in enumerate(deltas, start=1):
        tid = f"p{idx}"
        for arm, sc in (("baseline", 0.50), ("o1", 0.50 + d)):
            rd = out_flat / arm / tid / "r1"
            rd.mkdir(parents=True)
            (rd / "result.json").write_text(
                json.dumps(
                    {
                        "arm": arm,
                        "task": tid,
                        "r": 1,
                        "audit_ok": True,
                        "replay_ok": True,
                        "breaker": False,
                        "graded": True,
                        "score": sc,
                    }
                )
            )
    rep_flat = analyze_experiment(out_flat, n_boot=1000)
    assert rep_flat["kill_criterion_0_triggered"] is True
