"""Score a confkit workspace against the hidden acceptance suite.

Usage: python bench/team/minipkg/score.py <workspace_dir> [--json out.json]

<workspace_dir> is the directory that contains the `confkit/` package. Only a fresh temp copy
of hidden_tests/ is collected, so the workspace's own tests are never run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
HIDDEN = HERE / "hidden_tests"
REFERENCE = HERE / "reference"
TIMEOUT = 600


def _prepare(tmp: Path) -> Path:
    suite = tmp / "suite"
    shutil.copytree(HIDDEN, suite, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (suite / "pytest.ini").write_text("[pytest]\naddopts =\n", encoding="utf-8")
    return suite


def _pytest(suite: Path, package_root: Path, extra: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CONFKIT_", "PYTEST_"))}
    env["PYTHONPATH"] = str(package_root.resolve())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-c",
        str(suite / "pytest.ini"),
        "--rootdir",
        str(suite),
        *extra,
        str(suite),
    ]
    return subprocess.run(cmd, cwd=suite, env=env, capture_output=True, text=True, timeout=timeout)


def _expected_ids(tmp: Path) -> list[str]:
    """Collect test ids against the reference implementation (defines the full test count)."""
    suite = _prepare(tmp / "collect")
    proc = _pytest(suite, REFERENCE, ["--collect-only", "-q"], timeout=TIMEOUT)
    ids = [line.strip() for line in proc.stdout.splitlines() if "::" in line]
    if proc.returncode != 0 or not ids:
        raise SystemExit(f"cannot collect hidden tests against reference:\n{proc.stdout}\n{proc.stderr}")
    return ids


def _junit_outcomes(xml_path: Path) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    if not xml_path.exists():
        return outcomes
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        classname = case.get("classname", "")
        if not classname:
            continue  # collection errors are reported without a test id
        test_id = classname.replace(".", "/") + ".py::" + case.get("name", "")
        if case.find("error") is not None:
            outcome = "error"
        elif case.find("failure") is not None or case.find("skipped") is not None:
            outcome = "failed"
        else:
            outcome = "passed"
        # a test can appear twice (e.g. failure + teardown error); keep the worst outcome
        if outcomes.get(test_id) in (None, "passed"):
            outcomes[test_id] = outcome
    return outcomes


def score(workspace: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="confkit-score-") as tmp_name:
        tmp = Path(tmp_name)
        expected = _expected_ids(tmp)
        suite = _prepare(tmp / "run")
        xml_path = tmp / "junit.xml"
        note = ""
        try:
            proc = _pytest(suite, workspace, [f"--junitxml={xml_path}", "-q"], timeout=TIMEOUT)
            if proc.returncode not in (0, 1):
                note = (proc.stdout + proc.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            note = f"pytest timed out after {TIMEOUT}s"
        outcomes = _junit_outcomes(xml_path)

    counts = {"passed": 0, "failed": 0, "errors": 0}
    by_file: dict[str, list[int]] = {}
    failures: list[str] = []
    for test_id in expected:
        # tests that did not run (import failure, collection error, timeout) count as failed
        outcome = outcomes.get(test_id, "failed")
        counts["errors" if outcome == "error" else outcome] += 1
        file_stats = by_file.setdefault(test_id.split("::", 1)[0], [0, 0])
        file_stats[1] += 1
        if outcome == "passed":
            file_stats[0] += 1
        else:
            failures.append(test_id)
    total = len(expected)
    summary = {
        **counts,
        "total": total,
        "score": round(counts["passed"] / total, 4),
        "by_file": by_file,
        "failures": failures,
    }
    if note:
        summary["note"] = note
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("workspace", type=Path, help="directory containing the confkit/ package")
    parser.add_argument("--json", type=Path, dest="json_out", help="also write the summary to this file")
    args = parser.parse_args()
    if not args.workspace.is_dir():
        parser.error(f"not a directory: {args.workspace}")
    summary = score(args.workspace)
    text = json.dumps(summary, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
