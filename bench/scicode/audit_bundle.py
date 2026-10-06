"""Build the auditor-only evidence bundles (docs/60 §6.4): for every problem, the held-out case
(grade.audit_case) of each step with its numeric target from the verified HDF5, plus the check
script. Output: bench/scicode/audit/<problem_id>/{bundle.pkl, check.py}, mounted read-only into
the validation sandbox only.

Usage: python bench/scicode/audit_bundle.py
"""

import pickle
import shutil
import sys

import grade

OUT = grade.HERE / "audit"


def build(pid: str) -> int:
    sys.path.insert(0, str(grade.VENDOR))
    from scicode.parse.parse import process_hdf5_to_tuple

    prob = grade.problem(pid)
    steps, gold = [], {}
    for k, s in enumerate(prob["sub_steps"]):
        sid = s["step_number"]
        if (pid, k) in grade.SKIP:
            gold[sid] = grade.gold(sid)
            continue
        i = grade.audit_case(len(s["test_cases"]))
        if i is None:
            continue
        targets = process_hdf5_to_tuple(sid, len(s["test_cases"]), str(grade.H5))
        steps.append({"sid": sid, "case": s["test_cases"][i], "target": targets[i]})
    d = OUT / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "bundle.pkl").write_bytes(pickle.dumps(
        {"deps": prob["required_dependencies"], "gold": gold, "steps": steps}))
    shutil.copy(grade.HERE / "audit_check.py", d / "check.py")
    # the test cases import scicode.compare.cmp; the sandbox has no such package
    shutil.copytree(grade.VENDOR / "scicode" / "compare", d / "scicode" / "compare",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (d / "scicode" / "__init__.py").write_text("")
    return len(steps)


def main() -> None:
    pids = [str(p["problem_id"]) for p in map(__import__("json").loads,
                                              grade.DATA.read_text().splitlines())]
    total = 0
    for pid in pids:
        total += build(pid)
    print(f"{len(pids)} bundles, {total} audited steps -> {OUT}")


if __name__ == "__main__":
    main()
