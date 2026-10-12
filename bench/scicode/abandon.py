"""Registered analysis of x-abandon-pilot (docs/60 §6.7 item 7).

Per run: end reason, external score, reject count, whether the reviewer cancelled,
and whether an explicit continue/abandon decision appears in a reject or cancel note
after the third rejection. Recomputes (a)(b)(c) from the event store.

Usage: abandon.py EXPERIMENT_DIR --out DIR
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DECIDE = ("continue", "abandon", "cancel", "cancelling", "abandoning")


def run_row(d: Path) -> dict:
    res = json.loads((d / "result.json").read_text())
    score = json.loads((d / "score.json").read_text())["score"] if (d / "score.json").exists() else None
    db = sqlite3.connect(d / "events.db")
    notes = []
    for (p,) in db.execute("select payload from events where type='task_status_changed'"):
        ev = json.loads(p)
        if ev["transition"] in ("reject", "cancel"):
            notes.append((ev["transition"], ev.get("note") or ""))
    rejects = [n for t, n in notes if t == "reject"]
    cancel = next((n for t, n in notes if t == "cancel"), None)
    after = rejects[3:] + ([cancel] if cancel else [])
    decided = any(any(w in (n or "").lower() for w in DECIDE) for n in after) or bool(cancel)
    return {
        "task": d.parent.name, "repeat": d.name, "end": res["end_reason"],
        "score": None if score is None else round(score, 3),
        "cost_usd": round(res.get("cost_usd") or 0, 3),
        "rejects": len(rejects), "cancelled": cancel is not None, "decided": decided,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exp", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = [run_row(d) for d in sorted(a.exp.glob("heldout-abandon/*/r*")) if (d / "result.json").exists()]
    n = len(rows)
    br = sum(r["end"] == "breaker:budget" for r in rows)
    three = [r for r in rows if r["rejects"] >= 3]
    abandoned = [r for r in rows if r["cancelled"]]
    false_ab = [r for r in abandoned if r["score"] == 1]
    res = {
        "runs": n,
        "a_budget_breakers": br,
        "a_holds": br <= 4,
        "b_reached_three": len(three),
        "b_decided": sum(r["decided"] for r in three),
        "b_holds": (sum(r["decided"] for r in three) > len(three) / 2) if three else None,
        "c_abandoned": len(abandoned),
        "c_false_abandon": [f"{r['task']}/{r['repeat']}" for r in false_ab],
        "c_holds": len(false_ab) == 0,
        "rows": rows,
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "analysis.json").write_text(json.dumps(res, indent=2) + "\n")
    md = [
        f"# x-abandon-pilot ({n} runs)", "",
        f"- (a) budget breakers {br}/{n} (hold ≤4): {res['a_holds']}",
        f"- (b) decided after 3 rejects {res['b_decided']}/{res['b_reached_three']}: {res['b_holds']}",
        f"- (c) false abandon {res['c_false_abandon'] or 'none'}: {res['c_holds']}", "",
        "| task | r | end | score | rejects | cancel | decided | cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(f"| {r['task']} | {r['repeat']} | {r['end']} | {r['score']} | "
                  f"{r['rejects']} | {r['cancelled']} | {r['decided']} | {r['cost_usd']} |")
    text = "\n".join(md) + "\n"
    (a.out / "analysis.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
