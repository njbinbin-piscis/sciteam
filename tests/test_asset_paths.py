"""M-E2: O_ASSETS_ROOT resolution (sciteam/asset_paths.py) and its threading
through experiment_pack / hr paths / role catalog."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sciteam import asset_paths, experiment_pack, roster_policy

LAB_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def org_assets_copy(tmp_path):
    """A minimal but structurally complete asset copy, with one skill edited
    so reads-from-copy are distinguishable from reads-from-baseline."""
    root = tmp_path / "org_assets"
    for sub in ["skills", "prompts", "roles"]:
        shutil.copytree(LAB_ROOT / "assets" / sub, root / sub)
    marker = "MARKER-FROM-ORG-COPY"
    skill = root / "skills" / "lit.survey" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8")
    return root, marker


# ---------------------------------------------------------------------------
# Resolver semantics
# ---------------------------------------------------------------------------


def test_default_root_unchanged(monkeypatch):
    monkeypatch.delenv(asset_paths.ENV_VAR, raising=False)
    assert asset_paths.assets_root() == LAB_ROOT / "assets"
    assert asset_paths.skills_dir() == LAB_ROOT / "assets" / "skills"
    assert asset_paths.role_catalog_path() == LAB_ROOT / "assets" / "roles" / "CATALOG.yaml"


def test_env_override(monkeypatch, org_assets_copy):
    root, _ = org_assets_copy
    monkeypatch.setenv(asset_paths.ENV_VAR, str(root))
    assert asset_paths.assets_root() == root
    assert asset_paths.role_prompts_dir() == root / "prompts" / "roles"


def test_missing_root_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv(asset_paths.ENV_VAR, str(tmp_path / "nope"))
    with pytest.raises(asset_paths.AssetsRootError):
        asset_paths.assets_root()


def test_half_empty_root_fails_fast(monkeypatch, tmp_path):
    empty = tmp_path / "empty_root"
    empty.mkdir()
    monkeypatch.setenv(asset_paths.ENV_VAR, str(empty))
    with pytest.raises(asset_paths.AssetsRootError):
        asset_paths.assets_root()


def test_display_path_inside_and_outside_lab(tmp_path):
    inside = LAB_ROOT / "assets" / "skills" / "lit.survey" / "SKILL.md"
    assert asset_paths.display_path(inside) == "assets/skills/lit.survey/SKILL.md"
    outside = tmp_path / "x.md"
    assert asset_paths.display_path(outside) == str(outside)


# ---------------------------------------------------------------------------
# Threading through the pack / HR / catalog layers
# ---------------------------------------------------------------------------


def test_read_template_skill_baseline(monkeypatch):
    monkeypatch.delenv(asset_paths.ENV_VAR, raising=False)
    rec = experiment_pack.read_template_skill("lit.survey")
    assert rec["path"] == "assets/skills/lit.survey/SKILL.md"
    assert "MARKER-FROM-ORG-COPY" not in rec["text"]


def test_read_template_skill_from_copy(monkeypatch, org_assets_copy):
    root, marker = org_assets_copy
    monkeypatch.setenv(asset_paths.ENV_VAR, str(root))
    rec = experiment_pack.read_template_skill("lit.survey")
    assert marker in rec["text"]
    assert rec["path"] == str(root / "skills" / "lit.survey" / "SKILL.md")


def test_role_catalog_from_copy(monkeypatch, org_assets_copy):
    root, _ = org_assets_copy
    catalog_file = root / "roles" / "CATALOG.yaml"
    text = catalog_file.read_text(encoding="utf-8")
    text += (
        "\n  - id: org_test_role\n"
        '    title: "Org Test Role"\n'
        '    summary: "Only exists in the org copy."\n'
        "    default_skills: []\n"
    )
    catalog_file.write_text(text, encoding="utf-8")
    monkeypatch.setenv(asset_paths.ENV_VAR, str(root))
    catalog = roster_policy.load_role_catalog()
    assert "org_test_role" in catalog
    monkeypatch.delenv(asset_paths.ENV_VAR)
    assert "org_test_role" not in roster_policy.load_role_catalog()


def test_materialize_pack_from_copy(monkeypatch, tmp_path, org_assets_copy):
    root, marker = org_assets_copy
    hr_skill = root / "skills" / "hr.recruit" / "SKILL.md"
    hr_skill.write_text(hr_skill.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8")
    monkeypatch.setenv(asset_paths.ENV_VAR, str(root))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    experiment_pack.materialize_pack(run_dir)
    packed = run_dir / "pack" / "skills" / "hr.recruit" / "SKILL.md"
    assert packed.is_file() and marker in packed.read_text(encoding="utf-8")
