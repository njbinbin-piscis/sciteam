"""SciCode-Verified grader for the agent protocol (docs/60 §4.2).

Upstream asks a model for one step at a time and scores each cumulative prefix. Here a team
writes the whole solution in one file; this grader scores every step against that file, outside
the agents' sandbox, with the released numeric targets (never visible to the agents).

Same as upstream: targets from the verified HDF5, per-step test cases, a step passes if it is
correct in ANY of the pinned 2024-era / 2025-era environments, the three steps the official
harness skips are replaced by the reference code, a problem is correct only if every scored step
passes. Different from upstream: agents see all steps at once, may import any installed module,
and the code of later steps is present when an earlier step is tested. Scores are therefore not
comparable with the SciCode leaderboard.

Usage: python bench/scicode/grade.py <problem_id> <workspace> --json <out>
       [--only-cases 0,1] [--include-audit]
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
UP = HERE / "upstream"
DATA = UP / "scicode_verified" / "problems_test.jsonl"
H5 = UP / "scicode_verified" / "test_data_cleaned.h5"
VENDOR = UP / "eval_clean" / "vendor"
GOLD_DIR = VENDOR / "eval_data"
ENVS = [("2024", HERE / "envs" / "sci2024" / "bin" / "python"),
        ("2025", HERE / "envs" / "sci2025" / "bin" / "python")]
SKIP = {("13", 5), ("62", 0), ("76", 2)}       # (problem_id, step index) the official harness skips
AUDIT_MIN_CASES = 3                            # steps with fewer cases have no held-out case
TIMEOUT_S = 1800
MARK = "@@CASES@@"
SOLUTION = "solution.py"


def audit_case(n_cases: int) -> int | None:
    """Index of the case reserved for the auditor's validator (docs/60 §6.4): the last one,
    only for steps with at least AUDIT_MIN_CASES cases. Grading never uses it."""
    return n_cases - 1 if n_cases >= AUDIT_MIN_CASES else None


def problem(pid: str) -> dict:
    for line in DATA.read_text().splitlines():
        p = json.loads(line)
        if str(p["problem_id"]) == pid:
            return p
    raise SystemExit(f"unknown problem {pid}")


def gold(sid: str) -> str:
    return (GOLD_DIR / f"{sid}.txt").read_text()


def script(code: str, sid: str, cases: list[str], only: set[int] | None) -> str:
    """Run each test case on its own so one failure does not hide the others."""
    out = [code, "", "import sys, json as _json", f"sys.path.insert(0, r'{VENDOR.as_posix()}')",
           "from scicode.parse.parse import process_hdf5_to_tuple",
           f"targets = process_hdf5_to_tuple('{sid}', {len(cases)}, r'{H5.as_posix()}')",
           "_res = []"]
    for i, tc in enumerate(cases):
        if only is not None and i not in only:
            out.append("_res.append(None)")
            continue
        body = "\n".join("    " + ln for ln in tc.splitlines()) or "    pass"
        out += ["try:", f"    target = targets[{i}]", body, "    _res.append(True)",
                "except BaseException:", "    _res.append(False)"]
    out.append(f"print('{MARK}' + _json.dumps(_res))")
    return "\n".join(out)


def run_env(py: Path, text: str, n: int, tmp: Path) -> list[bool | None]:
    f = tmp / f"t_{abs(hash(text))}.py"
    f.write_text(text)
    try:
        p = subprocess.run([str(py), str(f)], capture_output=True, text=True, timeout=TIMEOUT_S,
                           cwd=tmp, env={"PATH": "/usr/bin:/bin", "OMP_NUM_THREADS": "1",
                                         "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    except subprocess.TimeoutExpired:
        return [False] * n
    for ln in reversed(p.stdout.splitlines()):
        if ln.startswith(MARK):
            return json.loads(ln[len(MARK):])
    return [False] * n                    # module-level error: nothing ran


def grade(pid: str, workspace: Path, only: set[int] | None = None,
          include_audit: bool = False) -> dict:
    prob = problem(pid)
    sol = workspace / SOLUTION
    agent = sol.read_text() if sol.exists() else ""
    given = [gold(s["step_number"]) for k, s in enumerate(prob["sub_steps"])
             if (pid, k) in SKIP]
    # Reference code for the skipped steps comes last so the agent cannot change it.
    code = "\n\n".join([prob["required_dependencies"], agent, *given])
    steps, scored = {}, 0
    with tempfile.TemporaryDirectory() as td:
        for k, s in enumerate(prob["sub_steps"]):
            if (pid, k) in SKIP or not s["test_cases"]:
                continue
            sid = s["step_number"]
            n = len(s["test_cases"])
            use = set(range(n)) if only is None else set(only)
            if not include_audit and audit_case(n) is not None:
                use.discard(audit_case(n))
            text = script(code, sid, s["test_cases"], use)
            per_env: dict[str, list[bool | None]] = {}
            for label, py in ENVS:
                per_env[label] = run_env(py, text, len(s["test_cases"]), Path(td))
                done = [c for c in per_env[label] if c is not None]
                if done and all(done):
                    break                 # one passing env is enough (the OR)
            ok = any(all(c for c in r if c is not None) and any(c is not None for c in r)
                     for r in per_env.values())
            steps[sid] = {"passed": ok, "per_env": per_env}
            scored += 1
    n_ok = sum(v["passed"] for v in steps.values())
    return {"problem_id": pid, "steps": steps, "n_scored": scored, "n_ok": n_ok,
            "passed": n_ok, "total": scored,
            "score": n_ok / scored if scored else 0.0,
            "problem_correct": bool(scored) and n_ok == scored,
            "solution_present": bool(agent.strip())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    ap.add_argument("--only-cases", help="comma-separated case indices to evaluate")
    ap.add_argument("--include-audit", action="store_true",
                    help="also use the held-out case (only to compare with pre-split scores)")
    a = ap.parse_args()
    only = {int(x) for x in a.only_cases.split(",")} if a.only_cases else None
    r = grade(a.pid, a.workspace.resolve(), only, a.include_audit)
    a.json.write_text(json.dumps(r, indent=2))
    print(json.dumps({k: r[k] for k in ("problem_id", "passed", "total", "score")}))


if __name__ == "__main__":
    sys.exit(main())
