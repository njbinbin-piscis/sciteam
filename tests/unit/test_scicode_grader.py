"""SciCode grader invariants (docs/60 §4.2, §6.4, deviation D6)."""

import json
import re
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2] / "bench" / "scicode"
sys.path.insert(0, str(BENCH))
import grade  # noqa: E402


def test_the_last_case_is_held_out_only_when_there_are_three_or_more():
    assert [grade.audit_case(n) for n in (0, 1, 2, 3, 5)] == [None, None, None, 2, 4]


@pytest.mark.skipif(not grade.DATA.exists(), reason="SciCode-Verified data not downloaded")
def test_every_scicode_module_a_test_case_imports_resolves_in_the_vendor_tree():
    """D6: scicode.compare was missing, so 23 steps could never pass and nothing said so."""
    wanted = set()
    for line in grade.DATA.read_text().splitlines():
        for s in json.loads(line)["sub_steps"]:
            for case in s["test_cases"]:
                wanted |= set(re.findall(r"^\s*from (scicode[\w.]*) import", case, re.M))
    assert wanted, "expected test cases to import from scicode"
    for mod in wanted:
        rel = Path(*mod.split("."))
        assert (grade.VENDOR / rel).with_suffix(".py").exists() or \
            (grade.VENDOR / rel / "__init__.py").exists(), f"{mod} is not in {grade.VENDOR}"
