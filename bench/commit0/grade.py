"""Grade a SciTeam workspace against a Commit0 library's upstream tests (docs/60 §3.3).

Same procedure and counting as `commit0 evaluate`: the workspace becomes a branch on top of
the library's base commit, the official harness applies its diff inside the official image
and runs the official test command; the score is (passed + xfail) / official test ids.
Differences, on purpose: the upstream tests are always the original ones (whatever the
agents did to test files is not part of the branch), and one library is graded at a time.

Usage (cwd anywhere): python bench/commit0/grade.py <lib> <workspace> --json <out>
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = "wentingzhao/commit0_combined"
EXCLUDE = [".git", "spec.pdf.bz2", "__pycache__", ".pytest_cache", "*.pyc", ".ruff_cache"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def drop_binary_changes(wt: Path) -> list[str]:
    """The official harness applies a text diff (`git apply`), which rejects the whole patch
    when it contains a binary change (e.g. a `.coverage` file the team left behind). Binary
    changes are never part of a source implementation: new ones are dropped, changed ones
    restored to base. Returns what was dropped."""
    dropped = []
    for line in git(wt, "diff", "--cached", "--numstat", "--no-renames", "HEAD").splitlines():
        added, removed, path = line.split("\t", 2)
        if added != "-" or removed != "-":
            continue
        in_base = subprocess.run(["git", "-C", str(wt), "cat-file", "-e", f"HEAD:{path}"],
                                 capture_output=True).returncode == 0
        if in_base:
            git(wt, "checkout", "HEAD", "--", path)
        else:
            git(wt, "rm", "--cached", "-q", "--", path)
        dropped.append(path)
    return dropped


def make_branch(lib: str, meta: dict, workspace: Path, branch: str) -> list[str]:
    repo = HERE / "repos" / lib
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        git(repo, "worktree", "add", "--detach", "-f", str(wt), meta["base_commit"])
        try:
            excl = [f"--exclude={x}" for x in EXCLUDE + meta["hidden"]]
            subprocess.run(["rsync", "-a", "--delete", *excl, f"{workspace}/", f"{wt}/"],
                           check=True)
            git(wt, "add", "-A")
            dropped = drop_binary_changes(wt)
            git(wt, "-c", "user.email=grader@sciteam", "-c", "user.name=grader", "commit",
                "--allow-empty", "-q", "-m", f"sciteam workspace {workspace}")
            git(repo, "branch", "-f", branch, git(wt, "rev-parse", "HEAD"))
        finally:
            git(repo, "worktree", "remove", "--force", str(wt))
    return dropped


def score(lib: str, meta: dict, branch: str, timeout: int) -> dict:
    from commit0.harness.constants import RUN_PYTEST_LOG_DIR
    from commit0.harness.get_pytest_ids import main as get_tests
    from commit0.harness.run_pytest_ids import main as run_tests
    from commit0.harness.utils import get_hash_string

    try:
        run_tests(DATASET, "test", str(HERE / "repos"), lib, branch, meta["test_dir"], False,
                  "local", timeout, 1, False, 0)
    except SystemExit:
        pass                                   # run_tests exits with pytest's code
    log_dir = HERE / RUN_PYTEST_LOG_DIR / lib / branch / get_hash_string(meta["test_dir"])
    ids = get_tests(lib, 0)
    report_file = log_dir / "report.json"
    tests = {}
    if report_file.exists():
        report = json.loads(report_file.read_text())
        tests = {t["nodeid"]: t["call"] for t in report.get("tests", []) if "call" in t}
    status = Counter(tests[t]["outcome"] if tests.get(t) else "failed" for t in ids)
    passed = status["passed"] + status["xfail"]
    return {"lib": lib, "branch": branch, "passed": passed, "total": len(ids),
            "score": passed / len(ids) if ids else 0.0, "status": dict(status),
            "report": str(report_file) if report_file.exists() else None,
            "log_dir": str(log_dir)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lib")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--branch")
    a = ap.parse_args()
    meta = json.loads((HERE / "seeds" / f"{a.lib}.json").read_text())
    ws = a.workspace.resolve()
    branch = a.branch or "sciteam-" + hashlib.sha256(str(ws).encode()).hexdigest()[:16]
    os.chdir(HERE)                            # the official harness logs under ./logs
    with open(HERE / f".grade-{a.lib}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        dropped = make_branch(a.lib, meta, ws, branch)
        result = {**score(a.lib, meta, branch, a.timeout), "dropped_binary": dropped}
    a.json.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("lib", "passed", "total", "score")}))
    sys.exit(0)


if __name__ == "__main__":
    main()
