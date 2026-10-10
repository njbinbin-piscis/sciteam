"""Offline analyses of SciCode experiment runs (docs/60 §6.5).

corr     Step-level failure correlation between independent runs (same model and across
         models): how much independent evidence a second solver can carry.
confirm  Summary of X-inst-confirm; runs ended by an infra fault (breaker:provider) are
         excluded; budget breakers are kept at their achieved score and reported separately.
breakers Mechanism check for budget breakers: special-validator verdicts, timeouts, and
         runs whose held-out feedback repeated the same failure three times in a row.

Each command writes <out>/analysis.json and <out>/analysis.md and prints the markdown.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

INFRA = {"breaker:provider"}


def load_steps(exp: Path) -> dict[tuple[str, int], dict[str, bool]]:
    """(pid, r) -> {step: passed} for every graded run of a single-arm experiment."""
    out = {}
    for f in sorted(exp.glob("single/p*/r*/score.json")):
        res = f.parent / "result.json"
        if res.exists() and json.loads(res.read_text())["end_reason"] in INFRA:
            continue
        s = json.loads(f.read_text())
        out[(s["problem_id"], int(f.parent.name[1:]))] = {k: bool(v["passed"]) for k, v in s["steps"].items()}
    return out


def pair_stats(a: dict, b: dict, pairs: list[tuple]) -> dict:
    n = fa = fb = both = 0
    for ka, kb in pairs:
        if ka not in a or kb not in b:
            continue
        for step, pa in a[ka].items():
            if step not in b[kb]:
                continue
            pb = b[kb][step]
            n += 1
            fa += not pa
            fb += not pb
            both += (not pa) and (not pb)
    if not n:
        return {"n": 0}
    pa_, pb_ = fa / n, fb / n
    return {
        "n": n,
        "p_fail_a": round(pa_, 3),
        "p_fail_b": round(pb_, 3),
        "p_b_fail_given_a_fail": round(both / fa, 3) if fa else None,
        "p_both": round(both / n, 3),
        "p_both_if_independent": round(pa_ * pb_, 3),
    }


def cmd_corr(models: dict[str, Path], out: Path) -> str:
    data = {m: load_steps(p) for m, p in models.items()}
    res: dict = {"runs": {m: len(d) for m, d in data.items()}, "pairs": {}}
    for m, d in data.items():
        pids = sorted({k[0] for k in d})
        res["pairs"][f"{m}~{m}"] = pair_stats(d, d, [((p, 1), (p, 2)) for p in pids])
    for m1, m2 in itertools.combinations(data, 2):
        pids = sorted({k[0] for k in data[m1]} & {k[0] for k in data[m2]})
        pairs = [((p, i), (p, j)) for p in pids for i in (1, 2) for j in (1, 2)]
        res["pairs"][f"{m1}~{m2}"] = pair_stats(data[m1], data[m2], pairs)
    steps: dict[str, list[bool]] = defaultdict(list)
    for d in data.values():
        for (_, _), st in d.items():
            for k, v in st.items():
                steps[k].append(v)
    full = {k: v for k, v in steps.items() if len(v) == 2 * len(data)}
    cat = Counter(
        "never_fail" if all(v) else "always_fail" if not any(v) else "sometimes" for v in full.values()
    )
    res["step_pattern"] = {"steps_with_all_runs": len(full), **cat}
    res["always_fail_steps"] = sorted(k for k, v in full.items() if not any(v))

    lines = [
        "# Step-level failure correlation (SciCode-Verified, held-out case excluded)",
        "",
        f"Runs per model: {res['runs']}",
        "",
        "| pair | steps | P(fail A) | P(fail B) | P(B fail given A fail) | P(both) | if independent |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, v in res["pairs"].items():
        if v["n"]:
            lines.append(
                f"| {k} | {v['n']} | {v['p_fail_a']} | {v['p_fail_b']} | "
                f"{v['p_b_fail_given_a_fail']} | {v['p_both']} | {v['p_both_if_independent']} |"
            )
    lines += ["", f"Step pattern across all runs: {res['step_pattern']}"]
    return write(out, res, lines)


def boot_ci(diffs: list[float], n: int = 10000, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n))
    return round(means[int(0.025 * n)], 3), round(means[int(0.975 * n)], 3)


def cmd_confirm(exp: Path, out: Path) -> str:
    runs, excluded = [], []
    for f in sorted(exp.glob("*/p*/r*/result.json")):
        r = json.loads(f.read_text())
        (excluded if r["end_reason"] in INFRA else runs).append(r)
    arms = sorted({r["arm"] for r in runs} | {r["arm"] for r in excluded})
    res: dict = {"valid": len(runs), "excluded_infra": len(excluded), "arms": {}}
    for a in arms:
        rs = [r for r in runs if r["arm"] == a]
        if not rs:
            continue
        res["arms"][a] = {
            "n": len(rs),
            "mean_score": round(sum(r["score"] for r in rs) / len(rs), 3),
            "end_reason": dict(Counter(r["end_reason"] for r in rs)),
            "closed_wrong": sum(r["closed"] and r["score"] < 1 for r in rs),
            "blocked_correct": sum((not r["closed"]) and r["score"] >= 1 for r in rs),
            "returned_runs": sum(r["returned"] > 0 for r in rs),
            "cost_usd": round(sum(r["cost_usd"] for r in rs), 2),
        }
    cell = {(r["arm"], r["task"], r["r"]): r["score"] for r in runs}
    keys = sorted({(t, rr) for _, t, rr in cell})
    res["paired"] = {}
    for a in arms:
        if a == "shared":
            continue
        d = [
            cell[(a, t, rr)] - cell[("shared", t, rr)]
            for t, rr in keys
            if (a, t, rr) in cell and ("shared", t, rr) in cell
        ]
        if d:
            res["paired"][f"{a}-shared"] = {
                "n_pairs": len(d),
                "mean": round(sum(d) / len(d), 3),
                "ci95_bootstrap": boot_ci(d),
            }
    lines = [
        "# X-inst-confirm summary",
        "",
        f"Valid runs: {res['valid']}; excluded as infra fault: {res['excluded_infra']}.",
        "",
        "| arm | n | mean score | closed but wrong | correct but not closed | runs with a return | cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for a, v in res["arms"].items():
        lines.append(
            f"| {a} | {v['n']} | {v['mean_score']} | {v['closed_wrong']} | "
            f"{v['blocked_correct']} | {v['returned_runs']} | ${v['cost_usd']} |"
        )
    lines += ["", "Paired by (problem, repeat), against `shared`:", ""]
    for k, v in res["paired"].items():
        lines.append(f"- {k}: {v['mean']} over {v['n_pairs']} pairs, 95% CI {v['ci95_bootstrap']}")
    return write(out, res, lines)


def cmd_breakers(exp: Path, out: Path) -> str:
    """Mechanism check for budget breakers: validator verdicts, timeouts, identical-feedback streaks."""
    per_arm: dict[str, Counter] = {}
    for f in sorted(exp.glob("*/p*/r*/result.json")):
        r = json.loads(f.read_text())
        c = per_arm.setdefault(r["arm"], Counter())
        c["runs"] += 1
        c["breakers"] += r["breaker"]
        ev = f.parent / "events.db"
        req: list[str] = []
        msgs: list[str] = []
        streak = best = 0
        con = sqlite3.connect(ev)
        for t, p in con.execute(
            "select type,payload from events where type in "
            "('validation_requested','validation_finished') order by rowid"
        ):
            j = json.loads(p)
            if t == "validation_requested":
                req.append(j.get("validator"))
                continue
            name = req.pop(0) if req else "?"
            if name not in ("heldout", "placebo"):
                continue
            c["special_verdicts"] += 1
            tail = j.get("tail") or ""
            if j.get("ok"):
                streak = 0
                continue
            c["special_fails"] += 1
            c["timeouts"] += "(timeout)" in tail
            msg = tail.split("held-out check:")[-1].strip()
            streak = streak + 1 if msgs and msgs[-1] == msg else 1
            best = max(best, streak)
            msgs.append(msg)
        con.close()
        c["runs_with_3_identical_fails"] += best >= 3
        if r["breaker"]:
            c["breaker_runs_with_3_identical_fails"] += best >= 3
    lines = [
        "# X-inst-confirm breaker diagnosis",
        "",
        "| arm | runs | budget breakers | special verdicts | special fails | timeouts "
        "| runs with ≥3 identical fails | of breakers |",
        "|---|---|---|---|---|---|---|---|",
    ]
    res: dict = {}
    for a, c in sorted(per_arm.items()):
        res[a] = dict(c)
        lines.append(
            f"| {a} | {c['runs']} | {c['breakers']} | {c['special_verdicts']} | "
            f"{c['special_fails']} | {c['timeouts']} | {c['runs_with_3_identical_fails']} | "
            f"{c['breaker_runs_with_3_identical_fails']} |"
        )
    return write(out, res, lines)


def write(out: Path, res: dict, lines: list[str]) -> str:
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n")
    md = "\n".join(lines) + "\n"
    (out / "analysis.md").write_text(md)
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("corr")
    c.add_argument("--model", action="append", required=True, help="name=experiment_dir")
    c.add_argument("--out", type=Path, required=True)
    f = sub.add_parser("confirm")
    f.add_argument("exp", type=Path)
    f.add_argument("--out", type=Path)
    b = sub.add_parser("breakers")
    b.add_argument("exp", type=Path)
    b.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cmd == "corr":
        models = dict(m.split("=", 1) for m in a.model)
        print(cmd_corr({k: Path(v) for k, v in models.items()}, a.out))
    elif a.cmd == "breakers":
        print(cmd_breakers(a.exp, a.out or a.exp / "analysis-breakers"))
    else:
        print(cmd_confirm(a.exp, a.out or a.exp / "analysis"))


if __name__ == "__main__":
    main()
