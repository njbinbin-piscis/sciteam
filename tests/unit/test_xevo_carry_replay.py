"""Unit tests for M1 X-evo sequential carry, hash-chain verification, and counterfactual replay."""

import json
from pathlib import Path

from sciteam.experiment.xevo import run_xevo_sequence, verify_carried_overlay
from sciteam.kernel.canonical import sha256_hex
from sciteam.kernel.events import EventStore


def _make_fake_runner(*, degrade_on_final: bool = False):
    """Build a deterministic fake runner that enacts an amendment at each step in `evolving`."""

    def runner(arm: str, task: str, run_dir: Path, overlays: list[Path]) -> dict:
        prev_text = "charter: v0\n"
        if overlays and (overlays[-1] / "org/x-evo.yaml").exists():
            prev_text = (overlays[-1] / "org/x-evo.yaml").read_text()

        store = EventStore(run_dir / "events.db")
        try:
            store.append(
                "institution",
                "rule_version_activated",
                {
                    "version": f"v0-{task}",
                    "files": {"org/x-evo.yaml": sha256_hex(prev_text)},
                    "texts": {"org/x-evo.yaml": prev_text},
                    "amendment": None,
                },
                actor="system",
            )
            if arm == "evolving":
                new_text = prev_text + f"# lesson from {task}\n"
                aid = f"A-{task}"
                store.append(
                    f"amendment/{aid}",
                    "amendment_enacted",
                    {
                        "amendment_id": aid,
                        "path": "org/x-evo.yaml",
                        "new_sha": sha256_hex(new_text),
                        "rule_version": f"v1-{task}",
                        "proposer": "lead",
                    },
                    actor="lead",
                )
                store.append(
                    "institution",
                    "rule_version_activated",
                    {
                        "version": f"v1-{task}",
                        "files": {"org/x-evo.yaml": sha256_hex(new_text)},
                        "texts": {"org/x-evo.yaml": new_text},
                        "amendment": aid,
                    },
                    actor="system",
                )
        finally:
            store.close()

        lessons_count = prev_text.count("# lesson from ")
        if arm == "frozen":
            score = 0.50
        elif arm == "evolving":
            score = 0.50 + 0.05 * lessons_count
        else:
            # Counterfactual replay with final overlay
            score = 0.40 if degrade_on_final else (0.50 + 0.05 * lessons_count)

        res = {"arm": arm, "task": task, "score": score, "lessons_seen": lessons_count}
        (run_dir / "result.json").write_text(json.dumps(res, indent=2))
        return res

    return runner


def test_xevo_sequence_carry_hash_chain_and_positive_replay(tmp_path):
    tasks = ["p8", "p12", "p13"]
    summary = run_xevo_sequence(
        tasks,
        tmp_path / "xevo_good",
        _make_fake_runner(degrade_on_final=False),
        replay_prefix_count=2,
    )

    assert summary["status"] == "promoted"
    assert summary["delta_replay"] > 0.10
    assert summary["evolving_mean_score"] > summary["frozen_mean_score"]
    assert len(summary["carry_ledger"]) == 3

    # Verify each step's CARRIED.json hash chain against its run's EventStore
    for entry in summary["carry_ledger"]:
        ov_dir = Path(entry["overlay_dir"])
        run_dir = tmp_path / "xevo_good" / "evolving" / entry["task"] / "r1"
        meta = verify_carried_overlay(run_dir, ov_dir)
        assert meta["rule_version"] == f"v1-{entry['task']}"
        assert len(meta["hash_chain"]) == entry["step"]

    final_org = (Path(summary["final_overlay"]) / "org/x-evo.yaml").read_text()
    for t in tasks:
        assert f"# lesson from {t}" in final_org


def test_xevo_counterfactual_replay_rejects_degraded_overlay(tmp_path):
    tasks = ["p8", "p12", "p13"]
    summary = run_xevo_sequence(
        tasks,
        tmp_path / "xevo_bad",
        _make_fake_runner(degrade_on_final=True),
        replay_prefix_count=2,
    )
    assert summary["delta_replay"] < 0.0
    assert summary["status"] == "rejected_degraded"
