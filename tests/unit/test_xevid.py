"""docs/60-experiment.md §6.6 v2: the X-evid agreement validator on synthetic solutions.

The validator never sees released targets; these tests use a tiny inputs-only bundle, so they
need no SciCode data. Each check runs three short subprocesses, so the file takes seconds."""

import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2] / "bench" / "xevid"
sys.path.insert(0, str(BENCH))
import agree  # noqa: E402

BUNDLE = {
    "problem_id": "toy",
    "head": "",
    "tail": "",
    "excluded": 0,
    "cases": [
        {"sid": "s1", "i": 0, "setup": "x = 3", "expr": "f(x)", "cmp": "abs(_a - target) < 1e-9"},
        {"sid": "s1", "i": 1, "setup": "x = 5", "expr": "f(x)", "cmp": "abs(_a - target) < 1e-9"},
    ],
}
GOOD = "def f(x):\n    return x * x\n"
WRONG = "def f(x):\n    return x * x + 1\n"
CRASH = "def f(x):\n    raise ValueError('boom')\n"
HANG = "import time\n\ndef f(x):\n    time.sleep(30)\n    return x\n"


def _check(a: str, b: str, bundle: dict = BUNDLE, timeout: float = 60) -> dict:
    return agree.check(bundle, a, b, sys.executable, timeout)


def test_two_correct_solutions_agree():
    rep = _check(GOOD, GOOD)
    assert rep["agree"] and rep["checked"] == 2 and rep["failed"] == []


def test_a_wrong_solution_is_caught_by_the_other():
    rep = _check(GOOD, WRONG)
    assert not rep["agree"]
    assert [f["reason"] for f in rep["failed"]] == ["mismatch", "mismatch"]


def test_two_identical_wrong_solutions_pass_the_known_residual_risk():
    # docs/60 §6.6: agreement cannot tell that both are wrong in the same way.
    rep = _check(WRONG, WRONG)
    assert rep["agree"]


@pytest.mark.parametrize("a,b,reason", [
    (CRASH, GOOD, "no_value_a"),
    (GOOD, CRASH, "no_value_b"),
])
def test_a_crash_counts_as_disagreement(a, b, reason):
    rep = _check(a, b)
    assert not rep["agree"]
    assert {f["reason"] for f in rep["failed"]} == {reason}


def test_a_timeout_counts_as_disagreement():
    rep = _check(HANG, GOOD, timeout=3)
    assert not rep["agree"]
    assert {f["reason"] for f in rep["failed"]} == {"no_value_a"}


def test_excluded_cases_are_reported_with_the_verdict():
    rep = _check(GOOD, GOOD, bundle={**BUNDLE, "excluded": 5})
    assert rep["agree"] and rep["excluded"] == 5


def test_cli_exit_code_follows_the_verdict(tmp_path, capsys):
    import json

    bundle = tmp_path / "b.json"
    bundle.write_text(json.dumps(BUNDLE))
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text(GOOD)
    b.write_text(WRONG)
    assert agree.main([str(bundle), str(a), str(b), "--python", sys.executable]) == 1
    b.write_text(GOOD)
    assert agree.main([str(bundle), str(a), str(b), "--python", sys.executable]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["agree"] is True


def test_bundle_carries_no_released_targets():
    import json

    import pytest as _pt

    import bundle
    import grade

    if not grade.DATA.exists():
        _pt.skip("SciCode data not present")
    b = bundle.build("13")
    text = json.dumps(b)
    assert "targets[" not in text and "hdf5" not in text.lower()
    assert len(b["cases"]) == 35 and b["excluded"] == 2


def test_missing_solution_file_is_a_disagreement(tmp_path):
    import json

    bundle = tmp_path / "b.json"
    bundle.write_text(json.dumps(BUNDLE))
    assert agree.main([str(bundle), str(tmp_path / "none.py"), str(tmp_path / "none.py"),
                       "--python", sys.executable]) == 1
