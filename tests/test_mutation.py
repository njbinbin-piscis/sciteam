"""Mutation testing of the institution loader.

The suite has two jobs, and the second is the one people forget. It must
show that breaking an institution is *caught* — and equally that harmless
edits are *not*, because a loader that rejects any change at all would post
a perfect kill rate while enforcing nothing.

Every bundled paradigm is held to a perfect score, so a future change that
weakens a load-time invariant fails here rather than in someone's run.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from sciteam._paths import ASSETS_ROOT
from sciteam.mutation import OPERATORS, MutationOperator, mutate, mutate_file
from sciteam.team_loader import parse_team_config

PARADIGMS = sorted((ASSETS_ROOT / "teams" / "paradigms").glob("*.yaml"))


@pytest.fixture
def manifest() -> dict:
    return yaml.safe_load((ASSETS_ROOT / "teams" / "paradigms" / "pipeline_chain.yaml").read_text())


class TestEveryBundledParadigm:
    @pytest.mark.parametrize("path", PARADIGMS, ids=lambda p: p.stem)
    def test_scores_perfectly(self, path):
        report = mutate_file(path)
        assert report.survivors == [], [f"{r.operator}: {r.description}" for r in report.survivors]
        assert report.false_kills == [], [f"{r.operator}: {r.detail}" for r in report.false_kills]
        assert report.score == 1.0

    def test_there_are_paradigms_to_test(self):
        assert len(PARADIGMS) >= 20


class TestOperatorsActuallyMutate:
    """A no-op operator would post a kill it did not earn."""

    @pytest.mark.parametrize("operator", OPERATORS, ids=lambda o: o.name)
    def test_each_applicable_operator_changes_the_manifest(self, manifest, operator):
        mutant = operator.apply(copy.deepcopy(manifest))
        if mutant is None:
            pytest.skip("not applicable to this paradigm")
        assert mutant != manifest

    def test_an_inapplicable_operator_is_not_counted(self):
        never = MutationOperator("X/none", "never applies", lambda m: None)
        report = mutate({"members": []}, operators=(never,))
        assert report.applicable == []
        assert report.score == 1.0  # nothing to kill, so nothing survived


class TestEquivalentMutantsMustSurvive:
    """The control condition: these change the text, not what it permits."""

    @pytest.mark.parametrize(
        "name", ["EQ/reorder-seats", "EQ/rewrite-prose", "EQ/annotate", "M15/erase-state"]
    )
    def test_harmless_edits_load(self, manifest, name):
        operator = next(o for o in OPERATORS if o.name == name)
        assert operator.should_be_killed is False
        parse_team_config(operator.apply(copy.deepcopy(manifest)), source="<eq>")

    def test_a_false_kill_is_reported_as_a_defect(self, manifest):
        # Simulate an over-strict loader: it rejects everything, including
        # the equivalent mutants. That must not read as a perfect score.
        def paranoid(_m):
            raise ValueError("no edits allowed")

        report = mutate(manifest, probe=paranoid)
        assert report.score == 1.0  # every non-equivalent mutant "killed"...
        assert report.false_kills  # ...but the report is still not ok
        assert not report.ok


class TestSurvivorsAreReported:
    def test_a_permissive_loader_leaves_survivors(self, manifest):
        report = mutate(manifest, probe=lambda _m: None)
        assert report.score == 0.0
        assert not report.ok
        assert {r.operator for r in report.survivors} >= {"M2/flip-separation", "M5/constantify"}

    def test_each_survivor_says_what_it_threatens(self, manifest):
        for result in mutate(manifest, probe=lambda _m: None).survivors:
            assert result.threatens.strip()


class TestRegressionsFoundByThisSuite:
    """Two gaps `sciteam mutate` found on first run; both are now invariants."""

    def test_an_institution_nobody_can_close_is_rejected(self, manifest):
        for member in manifest["members"]:
            member.pop("emits_exit_artifact", None)
        with pytest.raises(ValueError, match="no member holds emits_exit_artifact"):
            parse_team_config(manifest, source="<m1>")

    def test_a_negative_round_ceiling_is_rejected_not_clamped(self, manifest):
        manifest["coordination"]["stopping"]["max_iterations"] = -1
        with pytest.raises(ValueError, match="means \\*unlimited\\* rounds"):
            parse_team_config(manifest, source="<m10>")

    def test_zero_still_means_no_ceiling(self, manifest):
        # The trap this operator avoids: 0 is a legitimate sentinel, so
        # rejecting it would be strictness against a non-defect.
        manifest["coordination"]["stopping"]["max_iterations"] = 0
        assert parse_team_config(manifest, source="<m10b>").team_id
