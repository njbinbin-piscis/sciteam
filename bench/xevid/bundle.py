"""Build the X-evid agreement bundle for one SciCode problem (docs/60-experiment.md §6.6 v2).

The bundle holds inputs only: for every checkable case, the setup code, the value expression and
the official assertion with the value expression replaced by `_a`. It never reads the released
numeric targets (the HDF5 file is not opened), so the validator that consumes it cannot be shown
the answers. Cases whose assertion form is not recognised (split_case) are excluded and counted:
they cannot be compared, and the count is reported with every verdict.

Usage: bundle.py PROBLEM_ID --out bundle.json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scicode"))
import e3_agree  # noqa: E402
import grade  # noqa: E402


def build(pid: str) -> dict:
    prob = grade.problem(pid)
    given = [grade.gold(s["step_number"]) for k, s in enumerate(prob["sub_steps"])
             if (pid, k) in grade.SKIP]
    cases, excluded = [], 0
    for k, s in enumerate(prob["sub_steps"]):
        if (pid, k) in grade.SKIP or not s["test_cases"]:
            continue
        for i, tc in enumerate(s["test_cases"]):
            sp = e3_agree.split_case(tc)
            if sp is None:
                excluded += 1
                continue
            setup, expr = sp
            test = ast.unparse(ast.parse(tc).body[-1].test).replace(expr, "_a", 1)
            cases.append({"sid": s["step_number"], "i": i, "setup": setup, "expr": expr,
                          "cmp": test})
    return {"problem_id": pid, "head": prob["required_dependencies"],
            "tail": "\n\n".join(given), "cases": cases, "excluded": excluded}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("problem")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    b = build(a.problem)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(b, indent=2, ensure_ascii=False) + "\n")
    print(f"problem {a.problem}: {len(b['cases'])} checkable cases, {b['excluded']} excluded")


if __name__ == "__main__":
    main()
