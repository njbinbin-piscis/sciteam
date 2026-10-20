"""X-evid grader (docs/60-experiment.md §6.6 v2): score one run of the two-implementer team.

The organization's answer is implementer 1's solution, and it counts only when the question is
closed: both goal tasks were accepted by the agreement validator. A run that never closed has
answered nothing and scores 0. Both private solutions are graded with the SciCode grader
(bench/scicode/grade.py) so that the agreement check can be audited against correctness.

Usage: grade_run.py PROBLEM_ID WORKSPACE --json OUT
  WORKSPACE is the run's `workspace` directory; the private solutions sit in its sibling `private`.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scicode"))
sys.path.insert(0, str(HERE.parents[1]))
import grade  # noqa: E402

from sciteam.kernel.events import EventStore  # noqa: E402

IMPLEMENTERS = ("impl1", "impl2")


def closed(run: Path) -> bool:
    """Both goal tasks accepted. Goal tasks are the tasks labelled `goal` at creation."""
    store = EventStore(run / "events.db")
    try:
        evs = store.read_global()
    finally:
        store.close()
    created = [e.payload.get("task", e.payload) for e in evs if e.type == "task_created"]
    # only the owner's goal tasks count: a member cannot add or relabel one to change the verdict
    goals = {t["id"] for t in created if "goal" in (t.get("labels") or []) and t.get("created_by") == "owner"}
    accepted = {
        e.payload["subject"]
        for e in evs
        if e.type == "fact_established" and e.payload.get("fact") == "accepted"
    }
    return len(goals) == 2 and goals <= accepted


def grade_run(pid: str, workspace: Path) -> dict:
    run = workspace.parent
    per = {m: grade.grade(pid, run / "private" / m) for m in IMPLEMENTERS}
    is_closed = closed(run)
    answer = per["impl1"]
    score = answer["score"] if is_closed else 0.0
    return {
        "problem_id": pid,
        "closed": is_closed,
        "score": score,
        "passed": answer["n_ok"] if is_closed else 0,
        "total": answer["total"],
        "problem_correct": bool(is_closed and answer["problem_correct"]),
        "impl1": {k: per["impl1"][k] for k in ("score", "n_ok", "total", "problem_correct", "steps")},
        "impl2": {k: per["impl2"][k] for k in ("score", "n_ok", "total", "problem_correct", "steps")},
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("problem")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    a = ap.parse_args(argv)
    res = grade_run(a.problem, a.workspace.resolve())
    a.json.parent.mkdir(parents=True, exist_ok=True)
    a.json.write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({k: res[k] for k in ("closed", "score", "problem_correct")}))


if __name__ == "__main__":
    main()
