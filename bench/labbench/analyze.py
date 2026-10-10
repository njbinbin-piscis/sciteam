"""Registered analysis of x-base-labbench (docs/60 §4.4.1).

Per arm x subtask x variant: accuracy, precision (correct / answered), coverage (answered / all).
Per arm x SeqQA subtask: paired original - twin accuracy difference (pairs share a source item,
repeats averaged within the pair), 95% percentile bootstrap over pairs. Runs ended by a
provider breaker are excluded and counted.

Usage: analyze.py EXPERIMENT_DIR --out DIR
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

KEYS = Path(__file__).resolve().parent / "data" / "keys.json"
INFRA = {"breaker:provider"}
B = 10000


def load(exp: Path) -> tuple[list[dict], int]:
    keys = json.loads(KEYS.read_text())
    rows, infra = [], 0
    for f in sorted(exp.glob("*/*/r*/result.json")):
        res = json.loads(f.read_text())
        if res["end_reason"] in INFRA:
            infra += 1
            continue
        sf = f.parent / "score.json"
        s = json.loads(sf.read_text()) if sf.exists() else {"answered": False, "correct": False}
        item = f.parent.parent.name
        rows.append({"arm": f.parent.parent.parent.name, "item": item, **keys[item],
                     "answered": s["answered"], "correct": s["correct"]})
    return rows, infra


def rates(rs: list[dict]) -> dict:
    n, ans, ok = len(rs), sum(r["answered"] for r in rs), sum(r["correct"] for r in rs)
    return {"n": n, "accuracy": round(ok / n, 3) if n else None,
            "precision": round(ok / ans, 3) if ans else None, "coverage": round(ans / n, 3) if n else None}


def paired(rs: list[dict], seed: int = 0) -> dict:
    by = defaultdict(lambda: {"original": [], "perturbed": []})
    for r in rs:
        by[r["source_id"]][r["variant"]].append(r["correct"])
    d = [sum(v["original"]) / len(v["original"]) - sum(v["perturbed"]) / len(v["perturbed"])
         for v in by.values() if v["original"] and v["perturbed"]]
    if not d:
        return {"pairs": 0}
    rng = random.Random(seed)
    boots = sorted(sum(rng.choices(d, k=len(d))) / len(d) for _ in range(B))
    return {"pairs": len(d), "diff": round(sum(d) / len(d), 3),
            "ci95": [round(boots[int(0.025 * B)], 3), round(boots[int(0.975 * B) - 1], 3)]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exp", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows, infra = load(a.exp)
    cells = defaultdict(list)
    for r in rows:
        cells[(r["arm"], r["subtask"], r["variant"])].append(r)
    table = {"|".join(k): rates(v) for k, v in sorted(cells.items())}
    gaps = {}
    for arm in sorted({r["arm"] for r in rows}):
        for st in sorted({r["subtask"] for r in rows if r["variant"] != "unperturbed"}):
            gaps[f"{arm}|{st}"] = paired([r for r in rows if r["arm"] == arm and r["subtask"] == st])
    res = {"runs": len(rows), "infra_excluded": infra, "cells": table, "memorization_gap": gaps}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "analysis.json").write_text(json.dumps(res, indent=2) + "\n")
    md = [f"# x-base-labbench (runs {len(rows)}, provider-breaker excluded {infra})", "",
          "| arm | subtask | variant | n | accuracy | precision | coverage |", "|---|---|---|---|---|---|---|"]
    for k, v in table.items():
        md.append(f"| {' | '.join(k.split('|'))} | {v['n']} | {v['accuracy']} | {v['precision']} | {v['coverage']} |")
    md += ["", "| arm | subtask | pairs | original - twin | 95% CI |", "|---|---|---|---|---|"]
    for k, v in gaps.items():
        md.append(f"| {' | '.join(k.split('|'))} | {v['pairs']} | {v.get('diff')} | {v.get('ci95')} |")
    text = "\n".join(md) + "\n"
    (a.out / "analysis.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
