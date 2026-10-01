"""Summarise Harbor jobs, separating infrastructure-invalid trials from real agent results.

A trial is infra-invalid when the environment never came up, or the verifier could not run its
tests (it installs curl/uv/pytest at verify time, so network outages score 0 without testing
anything). Only valid trials count toward rates.

    python bench/tb/analyze.py runs/tb/<job> [runs/tb/<job> ...]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

INFRA_EXCEPTIONS = {"EnvironmentStartTimeoutError", "SandboxBuildFailedError",
                    "RewardFileNotFoundError", "VerifierTimeoutError"}
NET_MARKERS = ("Failed to fetch", "uv: command not found", "curl: command not found",
               "Failed to download", "tls handshake", "Temporary failure resolving",
               "Could not resolve host", "Connection timed out", "Unable to locate package curl")
# The host ran out of disk: the agent's own event store failed, not the agent.
HOST_MARKERS = ("No space left on device", "disk I/O error", "database or disk is full")


def classify(trial_dir: Path) -> dict | None:
    f = trial_dir / "result.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    if not d.get("finished_at"):
        return None
    exc = (d.get("exception_info") or {}).get("exception_type")
    info = d.get("exception_info") or {}
    exc_text = f"{info.get('exception_message') or ''}\n{info.get('exception_traceback') or ''}"
    reward = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    meta = (d.get("agent_result") or {}).get("metadata") or {}
    out = trial_dir / "verifier" / "test-stdout.txt"
    text = out.read_text(errors="replace") if out.exists() else ""
    if exc in INFRA_EXCEPTIONS or (exc == "RuntimeError" and not meta) or \
            any(m in exc_text for m in HOST_MARKERS):
        status = "infra"
    elif exc is not None:
        status = "agent_error"
    elif reward != 1.0 and any(m in text for m in NET_MARKERS):
        status = "infra"
    else:
        status = "valid"
    return {"task": d["task_name"].split("/")[-1], "status": status, "exc": exc,
            "reward": reward, "outcome": meta.get("outcome"), "steps": meta.get("steps"),
            "cost": (d.get("agent_result") or {}).get("cost_usd"), "trial": trial_dir.name}


def load(job: Path) -> list[dict]:
    return [r for t in sorted(job.iterdir()) if t.is_dir() and (r := classify(t))]


def main(jobs: list[str]) -> None:
    per_job = {}
    for j in jobs:
        rows = load(Path(j))
        per_job[j] = rows
        valid = [r for r in rows if r["status"] != "infra"]
        solved = sum(1 for r in valid if r["reward"] == 1.0)
        n_infra = len(rows) - len(valid)
        cost = sum(r["cost"] or 0 for r in rows)
        rate = solved / max(1, len(valid))
        print(f"{Path(j).name}: trials={len(rows)} infra={n_infra} valid={len(valid)} "
              f"solved={solved} rate={rate:.3f} cost=${cost:.2f}")
    if len(jobs) == 2:
        # Paired by task: mean valid-trial success per task, over tasks valid in both jobs.
        rates = []
        for j in jobs:
            m = defaultdict(list)
            for r in per_job[j]:
                if r["status"] != "infra":
                    m[r["task"]].append(1.0 if r["reward"] == 1.0 else 0.0)
            rates.append({t: sum(v) / len(v) for t, v in m.items()})
        common = sorted(set(rates[0]) & set(rates[1]))
        a = sum(rates[0][t] for t in common) / max(1, len(common))
        b = sum(rates[1][t] for t in common) / max(1, len(common))
        diffs = [rates[1][t] - rates[0][t] for t in common]
        n = len(diffs)
        mean = sum(diffs) / max(1, n)
        var = sum((x - mean) ** 2 for x in diffs) / max(1, n - 1)
        se = (var / max(1, n)) ** 0.5
        print(f"paired over {n} tasks: A={a:.3f} B={b:.3f} diff={mean:+.3f} ± {1.96 * se:.3f} (95%)")
        better = [t for t in common if rates[1][t] > rates[0][t]]
        worse = [t for t in common if rates[1][t] < rates[0][t]]
        print(f"B better on {len(better)}, worse on {len(worse)}")


if __name__ == "__main__":
    main(sys.argv[1:])
