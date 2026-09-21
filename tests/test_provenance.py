"""Provenance is only worth recording if it is honest about its own limits.

Two failure modes matter more than completeness here: a fingerprint that
claims reproducibility it cannot deliver, and a fingerprint that reports
drift for things that do not affect results (which teaches operators to
ignore it). Most of these tests are about those two.
"""

from __future__ import annotations

import json

import pytest
from sciteam.provenance import (
    REDACTED,
    compare,
    environment_fingerprint,
    fingerprint_digest,
    installed_plugins,
    sciteam_env_overrides,
    tracked_dependencies,
)
from sciteam.runtime_identity import IMAGE_DIGEST_ENV


@pytest.fixture
def clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("SCITEAM_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestFingerprint:
    def test_records_the_versions_that_actually_move(self, clean_env):
        pinned = environment_fingerprint()["pinned"]
        assert pinned["python"]["version"].count(".") == 2
        assert pinned["dependencies"]["pyyaml"] != "not-installed"
        assert pinned["dependencies"]["jsonschema"] != "not-installed"
        assert pinned["sciteam_version"]

    def test_the_digest_covers_the_pinned_half_only(self, clean_env):
        fp = environment_fingerprint()
        assert fp["pinned_digest"] == fingerprint_digest(fp["pinned"])
        # Install paths are recorded but must not move the digest, or two
        # identical containers on different mounts would look different.
        assert "data_root" in fp["context"]
        assert "data_root" not in json.dumps(fp["pinned"])

    def test_is_stable_across_calls(self, clean_env):
        assert (
            environment_fingerprint()["pinned_digest"]
            == (environment_fingerprint()["pinned_digest"])
        )


class TestHonestyAboutLimits:
    def test_an_unpinned_image_is_reported_as_uncontrolled(self, clean_env):
        fp = environment_fingerprint()
        assert fp["reproducible"] is False
        assert any(gap["what"] == "runtime image" for gap in fp["unpinnable"])

    def test_declaring_an_image_digest_pins_the_runtime(self, clean_env):
        clean_env.setenv(IMAGE_DIGEST_ENV, "sha256:abc123")
        fp = environment_fingerprint()
        assert fp["pinned"]["image_digest"] == "sha256:abc123"
        assert not any(gap["what"] == "runtime image" for gap in fp["unpinnable"])
        assert fp["reproducible"] is True

    def test_a_hosted_model_is_always_uncontrolled(self, clean_env):
        clean_env.setenv(IMAGE_DIGEST_ENV, "sha256:abc123")
        fp = environment_fingerprint(models=["deepseek-v4-pro"])
        # Even with everything else pinned: the vendor can change weights
        # behind a stable model id, so this can never claim reproducibility.
        assert fp["reproducible"] is False
        gap = next(g for g in fp["unpinnable"] if g["what"] == "model:deepseek-v4-pro")
        assert "without a version change" in gap["why"]

    def test_a_scripted_run_with_a_pinned_image_is_reproducible(self, clean_env):
        clean_env.setenv(IMAGE_DIGEST_ENV, "sha256:abc123")
        assert environment_fingerprint(models=[])["reproducible"] is True

    def test_every_gap_says_why(self, clean_env):
        for gap in environment_fingerprint(models=["m"])["unpinnable"]:
            assert gap["why"].strip()


class TestSecrets:
    def test_credential_values_never_reach_the_record(self, clean_env):
        clean_env.setenv("SCITEAM_LLM_API_KEY", "sk-do-not-leak")
        fp = environment_fingerprint()
        assert fp["pinned"]["env_overrides"]["SCITEAM_LLM_API_KEY"] == REDACTED
        assert "sk-do-not-leak" not in json.dumps(fp)

    def test_presence_is_still_recorded_because_it_changes_behaviour(self, clean_env):
        clean_env.setenv("SCITEAM_LLM_API_KEY", "sk-x")
        assert "SCITEAM_LLM_API_KEY" in sciteam_env_overrides()

    @pytest.mark.parametrize(
        "name",
        ["SCITEAM_X_TOKEN", "SCITEAM_A_SECRET", "SCITEAM_DB_PASSWORD", "SCITEAM_MY_CREDENTIAL"],
    )
    def test_other_credential_shapes_are_redacted_too(self, clean_env, name):
        clean_env.setenv(name, "leak")
        assert sciteam_env_overrides()[name] == REDACTED

    def test_non_secret_overrides_keep_their_values(self, clean_env):
        clean_env.setenv("SCITEAM_MAX_ITERATIONS", "9")
        assert sciteam_env_overrides()["SCITEAM_MAX_ITERATIONS"] == "9"

    def test_unrelated_host_env_is_not_captured(self, clean_env):
        clean_env.setenv("HOME_SECRET_THING", "x")
        assert "HOME_SECRET_THING" not in sciteam_env_overrides()


class TestDriftSignal:
    def test_output_location_is_not_treated_as_drift(self, clean_env):
        before = environment_fingerprint()
        clean_env.setenv("SCITEAM_RUNS_ROOT", "/tmp/elsewhere")
        after = environment_fingerprint()
        # Same experiment, different output dir: must not be flagged.
        assert compare(before, after) == []
        assert after["context"]["output_env"]["SCITEAM_RUNS_ROOT"] == "/tmp/elsewhere"

    def test_a_different_asset_root_is_drift(self, clean_env):
        before = environment_fingerprint()
        clean_env.setenv("SCITEAM_ASSETS_ROOT", "/tmp/other-institution")
        # A different asset root is a different institution — never silent.
        assert compare(before, environment_fingerprint()) == [
            "env_overrides.SCITEAM_ASSETS_ROOT: None -> '/tmp/other-institution'"
        ]

    def test_compare_names_the_covariate_that_moved(self, clean_env):
        before = environment_fingerprint()
        clean_env.setenv("SCITEAM_MAX_ITERATIONS", "9")
        diffs = compare(before, environment_fingerprint())
        assert diffs == ["env_overrides.SCITEAM_MAX_ITERATIONS: None -> '9'"]

    def test_compare_reports_nested_version_changes(self):
        left = {"pinned": {"dependencies": {"pyyaml": "6.0.1"}}}
        right = {"pinned": {"dependencies": {"pyyaml": "6.0.3"}}}
        assert compare(left, right) == ["dependencies.pyyaml: '6.0.1' -> '6.0.3'"]

    def test_compare_of_identical_environments_is_empty(self, clean_env):
        assert compare(environment_fingerprint(), environment_fingerprint()) == []


class TestLookups:
    def test_tracked_dependencies_covers_the_declared_runtime_deps(self):
        assert set(tracked_dependencies()) == {"sciteam", "pyyaml", "jsonschema"}

    def test_plugin_discovery_is_side_effect_free(self):
        # Reads entry-point metadata only; must never import a plugin, since
        # this runs while recording provenance.
        assert isinstance(installed_plugins(), list)
