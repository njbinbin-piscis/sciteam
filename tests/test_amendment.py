"""M-E1: unit tests for the Grundnorm G7 amendment kernel (sciteam/amendment.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sciteam import amendment

LAB_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = LAB_ROOT / "schemas"


def make_proposal(**overrides):
    base = {
        "amendment_id": "amd_test_001",
        "target_asset": "teams/paradigms/toy.yaml",
        "change_kind": "modify",
        "diff": "@@ -1,1 +1,1 @@\n-old\n+new",
        "motivation": {
            "failure_refs": ["runs/fake/missions/m1/results.json"],
            "pattern": "stall after round 3",
        },
        "prediction": {
            "metric": "contract_ok_rate",
            "direction": "+",
            "scope": "toy family",
            "falsifier": "shadow contract_ok drops vs incumbent",
        },
        "rollback": "revert diff",
        "proposer": "retro_moderator",
        "status": "drafted",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Layer derivation (fail-closed whitelist)
# ---------------------------------------------------------------------------

# Literal copy of the frozen table in lineE/plans/M-E0-plan.md §4, extended
# by the line F addendum in lineF/plans/M-F1-plan.md §4 (new prefix only;
# no pre-existing row's layer changed) — the doc<->code consistency lock.
# Update both places together or this fails.
DOCUMENTED_WHITELIST = [
    ("skills/lit.survey/SKILL.md", "P"),
    ("prompts/roles/analyst.md", "P"),
    ("teams/paradigms/debate_elo.yaml", "L"),
    ("templates/co_scientist_spine.plan.json", "L"),
    ("roles/CATALOG.yaml", "L"),
    ("campaigns/protocol_template.yaml", "L"),
    ("prompts/tasks/brief.md", "L"),
    ("prompts/constitution.md", "C"),
    ("institutions/GRUNDNORM.md", "G"),
    ("breeding/fitness_criteria.yaml", "L"),  # line F addendum (M-F1-plan.md §4)
]


@pytest.mark.parametrize("path,layer", DOCUMENTED_WHITELIST)
def test_derive_layer_matches_frozen_doc(path, layer):
    assert amendment.derive_layer(path) == layer


@pytest.mark.parametrize(
    "path",
    [
        "schemas/results.schema.json",  # outside org asset root by design
        "sciteam/amendment.py",
        "../escape/skills/x.md",
        "/abs/skills/x.md",
        "skills\\win\\style.md",
        "",
        "prompts/other.md",
    ],
)
def test_derive_layer_fail_closed(path):
    assert amendment.derive_layer(path) is None


# ---------------------------------------------------------------------------
# 2. Preflight
# ---------------------------------------------------------------------------


def test_preflight_ok_and_layer():
    res = amendment.preflight(make_proposal(), SCHEMAS_DIR)
    assert res.ok and res.layer == "L" and not res.flags


def test_preflight_schema_reject():
    bad = make_proposal()
    del bad["prediction"]
    res = amendment.preflight(bad, SCHEMAS_DIR)
    assert not res.ok and any(r.startswith("schema:") for r in res.reasons)


def test_preflight_entrenchment():
    res = amendment.preflight(make_proposal(target_asset="institutions/GRUNDNORM.md"), SCHEMAS_DIR)
    assert not res.ok and res.reasons == ["entrenchment"] and res.layer == "G"


def test_preflight_off_whitelist():
    res = amendment.preflight(make_proposal(target_asset="secret/rules.yaml"), SCHEMAS_DIR)
    assert not res.ok and res.reasons == ["off_whitelist"]


def test_preflight_declared_mismatch_flags_not_rejects():
    res = amendment.preflight(make_proposal(declared_layer="P"), SCHEMAS_DIR)
    assert res.ok and res.layer == "L"
    assert any("declared_layer_mismatch" in f for f in res.flags)


# ---------------------------------------------------------------------------
# 3. Ledger state machine
# ---------------------------------------------------------------------------


def test_ledger_legal_chain(tmp_path):
    for status in ["drafted", "shadow_pass", "promoted", "rolled_back"]:
        amendment.append_ledger(tmp_path, {"amendment_id": "amd_a", "status": status})
    entries = amendment.read_ledger(tmp_path)
    assert [e["status"] for e in entries] == ["drafted", "shadow_pass", "promoted", "rolled_back"]
    assert all("ts" in e for e in entries)


def test_ledger_illegal_jump(tmp_path):
    amendment.append_ledger(tmp_path, {"amendment_id": "amd_b", "status": "drafted"})
    with pytest.raises(amendment.LedgerError):
        amendment.append_ledger(tmp_path, {"amendment_id": "amd_b", "status": "promoted"})


def test_ledger_terminal_is_final(tmp_path):
    amendment.append_ledger(tmp_path, {"amendment_id": "amd_c", "status": "drafted"})
    amendment.append_ledger(tmp_path, {"amendment_id": "amd_c", "status": "rejected"})
    with pytest.raises(amendment.LedgerError):
        amendment.append_ledger(tmp_path, {"amendment_id": "amd_c", "status": "shadow_pass"})


def test_ledger_ungoverned_needs_none_governance(tmp_path):
    with pytest.raises(amendment.LedgerError):
        amendment.append_ledger(tmp_path, {"amendment_id": "amd_d", "status": "ungoverned_applied"})
    rec = amendment.append_ledger(
        tmp_path, {"amendment_id": "amd_d", "status": "ungoverned_applied"}, governance="none"
    )
    assert rec["status"] == "ungoverned_applied"


def test_ledger_append_only_growth(tmp_path):
    amendment.append_ledger(tmp_path, {"amendment_id": "amd_e", "status": "drafted"})
    n1 = len(amendment.ledger_path(tmp_path).read_text().splitlines())
    amendment.append_ledger(tmp_path, {"amendment_id": "amd_e", "status": "rejected"})
    n2 = len(amendment.ledger_path(tmp_path).read_text().splitlines())
    assert (n1, n2) == (1, 2)


# ---------------------------------------------------------------------------
# 4. Apply (add / modify / remove) with loadability guard
# ---------------------------------------------------------------------------


@pytest.fixture()
def org_assets(tmp_path):
    root = tmp_path / "assets"
    (root / "teams" / "paradigms").mkdir(parents=True)
    (root / "teams" / "paradigms" / "toy.yaml").write_text(
        "id: toy\nname: Toy Paradigm\nstagnation_max_rounds: 8\n", encoding="utf-8"
    )
    (root / "skills").mkdir()
    return root


def test_apply_add_skill(org_assets):
    proposal = make_proposal(
        amendment_id="amd_add",
        target_asset="skills/org.retro/SKILL.md",
        change_kind="add",
        diff="# org.retro\n\nRun the retrospective honestly.\n",
    )
    rec = amendment.apply_amendment(org_assets, proposal)
    assert (org_assets / "skills/org.retro/SKILL.md").exists()
    assert rec.before_sha256 is None and rec.after_sha256


def test_apply_modify_yaml(org_assets):
    diff = "@@ -3,1 +3,1 @@\n-stagnation_max_rounds: 8\n+stagnation_max_rounds: 5\n"
    proposal = make_proposal(change_kind="modify", diff=diff)
    rec = amendment.apply_amendment(org_assets, proposal)
    data = yaml.safe_load((org_assets / "teams/paradigms/toy.yaml").read_text())
    assert data["stagnation_max_rounds"] == 5
    assert rec.before_sha256 != rec.after_sha256


def test_apply_modify_context_mismatch_keeps_original(org_assets):
    target = org_assets / "teams/paradigms/toy.yaml"
    before = target.read_text()
    diff = "@@ -3,1 +3,1 @@\n-stagnation_max_rounds: 99\n+stagnation_max_rounds: 5\n"
    with pytest.raises(amendment.ApplyError):
        amendment.apply_amendment(org_assets, make_proposal(change_kind="modify", diff=diff))
    assert target.read_text() == before


def test_apply_modify_broken_yaml_restored(org_assets):
    target = org_assets / "teams/paradigms/toy.yaml"
    before = target.read_text()
    diff = "@@ -1,1 +1,1 @@\n-id: toy\n+id: [unclosed\n"
    with pytest.raises(yaml.YAMLError):
        amendment.apply_amendment(org_assets, make_proposal(change_kind="modify", diff=diff))
    assert target.read_text() == before


def test_apply_remove(org_assets):
    proposal = make_proposal(change_kind="remove", diff="")
    rec = amendment.apply_amendment(org_assets, proposal)
    assert not (org_assets / "teams/paradigms/toy.yaml").exists()
    assert rec.after_sha256 is None


def test_apply_refuses_g_layer(org_assets):
    (org_assets / "institutions").mkdir()
    (org_assets / "institutions" / "GRUNDNORM.md").write_text("g\n")
    proposal = make_proposal(
        target_asset="institutions/GRUNDNORM.md", change_kind="remove", diff=""
    )
    with pytest.raises(amendment.ApplyError):
        amendment.apply_amendment(org_assets, proposal)


# ---------------------------------------------------------------------------
# 5. Institution version hash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4b. Mechanical rollback (line A close-loop gap #3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def org_dir(tmp_path):
    """`org_dir/assets/...` + `org_dir/amendments.jsonl` — the layout
    `harness/run_org_series.py` actually uses, unlike the `org_assets`
    fixture above which points straight at the asset root."""
    root = tmp_path / "org"
    (root / "assets" / "teams" / "paradigms").mkdir(parents=True)
    (root / "assets" / "teams" / "paradigms" / "toy.yaml").write_text(
        "id: toy\nname: Toy Paradigm\nstagnation_max_rounds: 8\n", encoding="utf-8"
    )
    return root


def _promote_via_apply(org_dir: Path, proposal: dict) -> None:
    archive_dir = org_dir / "amendments_archive"
    record = amendment.apply_amendment(org_dir / "assets", proposal, archive_dir=archive_dir)
    amendment.append_ledger(
        org_dir, {"amendment_id": proposal["amendment_id"], "status": "drafted"}
    )
    amendment.append_ledger(
        org_dir, {"amendment_id": proposal["amendment_id"], "status": "shadow_pass"}
    )
    amendment.append_ledger(
        org_dir,
        {
            "amendment_id": proposal["amendment_id"],
            "status": "promoted",
            "before_sha256": record.before_sha256,
            "after_sha256": record.after_sha256,
        },
    )


def test_rollback_modify_restores_prior_content(org_dir):
    diff = "@@ -3,1 +3,1 @@\n-stagnation_max_rounds: 8\n+stagnation_max_rounds: 5\n"
    proposal = make_proposal(amendment_id="amd_rb1", change_kind="modify", diff=diff)
    _promote_via_apply(org_dir, proposal)
    target = org_dir / "assets" / "teams/paradigms/toy.yaml"
    assert "stagnation_max_rounds: 5" in target.read_text()

    entry = amendment.rollback_amendment(
        org_dir, org_dir / "amendments_archive", "amd_rb1", reason="regressed metric"
    )
    assert entry["status"] == "rolled_back"
    assert "stagnation_max_rounds: 8" in target.read_text()
    statuses = [
        e["status"] for e in amendment.read_ledger(org_dir) if e["amendment_id"] == "amd_rb1"
    ]
    assert statuses[-1] == "rolled_back"


def test_rollback_add_deletes_file(org_dir):
    proposal = make_proposal(
        amendment_id="amd_rb2",
        target_asset="skills/org.retro/SKILL.md",
        change_kind="add",
        diff="# org.retro\n",
    )
    _promote_via_apply(org_dir, proposal)
    target = org_dir / "assets" / "skills/org.retro/SKILL.md"
    assert target.is_file()
    amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb2")
    assert not target.exists()


def test_rollback_remove_recreates_file(org_dir):
    proposal = make_proposal(amendment_id="amd_rb3", change_kind="remove", diff="")
    before_text = (org_dir / "assets" / "teams/paradigms/toy.yaml").read_text()
    _promote_via_apply(org_dir, proposal)
    target = org_dir / "assets" / "teams/paradigms/toy.yaml"
    assert not target.exists()
    amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb3")
    assert target.read_text() == before_text


def test_rollback_refuses_when_not_promoted(org_dir):
    amendment.append_ledger(org_dir, {"amendment_id": "amd_rb4", "status": "drafted"})
    amendment.append_ledger(org_dir, {"amendment_id": "amd_rb4", "status": "rejected"})
    with pytest.raises(amendment.LedgerError):
        amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb4")


def test_rollback_refuses_without_archive(org_dir):
    amendment.append_ledger(org_dir, {"amendment_id": "amd_rb5", "status": "drafted"})
    amendment.append_ledger(org_dir, {"amendment_id": "amd_rb5", "status": "shadow_pass"})
    amendment.append_ledger(org_dir, {"amendment_id": "amd_rb5", "status": "promoted"})
    with pytest.raises(amendment.AmendmentError):
        amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb5")


def test_rollback_refuses_on_drift(org_dir):
    diff = "@@ -3,1 +3,1 @@\n-stagnation_max_rounds: 8\n+stagnation_max_rounds: 5\n"
    proposal = make_proposal(amendment_id="amd_rb6", change_kind="modify", diff=diff)
    _promote_via_apply(org_dir, proposal)
    target = org_dir / "assets" / "teams/paradigms/toy.yaml"
    # A second, later amendment (or a hand-edit) touched the same file since
    # promotion — mechanical rollback must refuse rather than clobber it.
    target.write_text("id: toy\nname: Toy Paradigm\nstagnation_max_rounds: 5\nextra: true\n")
    with pytest.raises(amendment.AmendmentError):
        amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb6")


def test_rollback_is_a_legal_terminal_transition_only_once(org_dir):
    diff = "@@ -3,1 +3,1 @@\n-stagnation_max_rounds: 8\n+stagnation_max_rounds: 5\n"
    proposal = make_proposal(amendment_id="amd_rb7", change_kind="modify", diff=diff)
    _promote_via_apply(org_dir, proposal)
    amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb7")
    with pytest.raises(amendment.LedgerError):
        amendment.rollback_amendment(org_dir, org_dir / "amendments_archive", "amd_rb7")


def test_institution_version_sensitivity(org_assets):
    v1 = amendment.institution_version(org_assets)
    assert v1.startswith("iv_") and v1 == amendment.institution_version(org_assets)
    (org_assets / "teams/paradigms/toy.yaml").write_text("id: toy2\n", encoding="utf-8")
    assert amendment.institution_version(org_assets) != v1


# ---------------------------------------------------------------------------
# 6. Motivation alignment audit
# ---------------------------------------------------------------------------


def test_audit_alignment(tmp_path):
    (tmp_path / "runs/fake/missions/m1").mkdir(parents=True)
    (tmp_path / "runs/fake/missions/m1/results.json").write_text("{}")
    ok = make_proposal()
    assert amendment.audit_alignment(ok, tmp_path) == []
    bad = make_proposal(
        motivation={"failure_refs": ["runs/nope.json", "../etc/passwd"], "pattern": "x"}
    )
    violations = amendment.audit_alignment(bad, tmp_path)
    assert any(v.startswith("unresolvable_ref:") for v in violations)
    assert any(v.startswith("unsafe_ref:") for v in violations)
