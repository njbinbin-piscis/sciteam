"""Unit tests for M1 X-evo organizational evolution config and protected_rules enforcement."""

from pathlib import Path

import pytest
import yaml

import sciteam.org.model  # noqa: F401 - registers FILE_KINDS["org/"] = Manifest
from sciteam.experiment.spec import load_spec
from sciteam.institution.model import RuleSet
from sciteam.kernel.config_store import ConfigStore

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"


def _load_xevo_ruleset() -> RuleSet:
    cs = ConfigStore(CONFIGS)
    texts = {
        "institution/x-evo.yaml": cs.load_text("institution/x-evo.yaml"),
        "institution/x-evo-rules.yaml": cs.load_text("institution/x-evo-rules.yaml"),
        "comm/x-inst.yaml": cs.load_text("comm/x-inst.yaml"),
        "org/x-evo.yaml": cs.load_text("org/x-evo.yaml"),
    }
    return RuleSet("institution/x-evo.yaml", "comm/x-inst.yaml", texts)


def test_xevo_smoke_spec_and_ruleset_load_cleanly():
    cs = ConfigStore(CONFIGS)
    spec, ts = load_spec(cs, "experiments/x-evo-smoke.yaml")
    assert [a.name for a in spec.arms] == ["frozen", "evolving"]
    assert len(ts.tasks) == 2

    rs = _load_xevo_ruleset()
    assert rs.constitution is not None
    assert "org/x-evo.yaml" in rs.extra
    manifest = rs.extra["org/x-evo.yaml"]
    assert [s.name for s in manifest.seats] == ["lead", "implementer", "auditor"]


def test_valid_amendments_to_org_manifest_and_non_protected_rules_succeed():
    rs = _load_xevo_ruleset()

    # 1. Amending Seat.charter and Seat.harness in org/x-evo.yaml succeeds
    org_data = yaml.safe_load(rs.texts["org/x-evo.yaml"])
    org_data["seats"][1]["charter"] += "\nAlways check numpy float64 dtypes before submitting."
    org_data["seats"][1]["harness"] = ["harness/exp-compact-128k.yaml"]
    new_org_text = yaml.safe_dump(org_data, sort_keys=False)
    rs2 = rs.apply_amendment("org/x-evo.yaml", new_org_text)
    assert "float64" in rs2.extra["org/x-evo.yaml"].seat("implementer").charter
    assert rs2.extra["org/x-evo.yaml"].seat("implementer").harness == ["harness/exp-compact-128k.yaml"]

    # 2. Amending a non-protected rule (subtask-has-reviewer) or adding a new rule succeeds
    rules_data = yaml.safe_load(rs.texts["institution/x-evo-rules.yaml"])
    rules_data["requirements"] = [
        r for r in rules_data["requirements"] if r["name"] != "subtask-has-reviewer"
    ]
    rules_data["requirements"].append(
        {
            "name": "subtask-has-acceptance",
            "act": "task_create",
            "where": {"task_is_root": False},
            "require": [{"field": "acceptance"}],
        }
    )
    new_rules_text = yaml.safe_dump(rules_data, sort_keys=False)
    rs3 = rs2.apply_amendment("institution/x-evo-rules.yaml", new_rules_text)
    assert "subtask-has-acceptance" in rs3.rules.named()


def test_unauthorized_amendments_to_protected_rules_or_harness_are_rejected():
    rs = _load_xevo_ruleset()

    # 1. Deleting a protected rule ('four-eyes') raises ValueError
    rules_data = yaml.safe_load(rs.texts["institution/x-evo-rules.yaml"])
    rules_data["requirements"] = [
        r for r in rules_data["requirements"] if r["name"] != "four-eyes"
    ]
    with pytest.raises(ValueError, match="protected rules"):
        rs.apply_amendment("institution/x-evo-rules.yaml", yaml.safe_dump(rules_data, sort_keys=False))

    # 2. Hollowing out the inline validator command used by protected rule 'work-validated' raises ValueError
    rules_hollow = yaml.safe_load(rs.texts["institution/x-evo-rules.yaml"])
    rules_hollow["validators"]["acceptance"]["command"] = "true"
    with pytest.raises(ValueError, match="protected rules"):
        rs.apply_amendment("institution/x-evo-rules.yaml", yaml.safe_dump(rules_hollow, sort_keys=False))

    # 3. Attempting to amend harness/*.yaml or constitution directly is rejected
    with pytest.raises(ValueError, match="not amendable"):
        rs.apply_amendment("harness/team-org.yaml", "name: hacked\nmodel: fake\n")
    with pytest.raises(ValueError, match="not amendable"):
        rs.apply_amendment("institution/x-evo.yaml", "name: hacked\n")
