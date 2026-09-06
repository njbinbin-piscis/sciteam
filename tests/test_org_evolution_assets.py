"""M-E3: line E institutional assets — paradigms parse, seats resolve,
skills registered."""

from __future__ import annotations

from pathlib import Path

import yaml
from sciteam.roster_policy import load_role_catalog, role_registry_preflight
from sciteam.team_loader import load_team_config

LAB_ROOT = Path(__file__).resolve().parents[1]
ASSETS = LAB_ROOT / "assets"

NEW_PARADIGMS = ["retro_council", "amendment_review"]
NEW_SKILLS = ["org.retro", "org.amend_draft", "org.amend_review", "org.shadow_verify", "org.evolve"]
NEW_ROLES = ["retro_moderator", "amendment_reviewer"]


def test_paradigms_load_and_emit_exit():
    for pid in NEW_PARADIGMS:
        cfg = load_team_config(ASSETS / "teams" / "paradigms" / f"{pid}.yaml")
        assert cfg.team_id == pid
        emitters = [m for m in cfg.members if m.emits_exit_artifact]
        assert len(emitters) == 1, f"{pid}: exactly one exit-artifact seat expected"
        assessors = [m for m in cfg.members if "may_assess_round" in (m.authority or [])]
        assert len(assessors) == 1, f"{pid}: exactly one round assessor expected"


def test_all_seats_resolve_to_catalog_and_prompts():
    catalog = load_role_catalog()
    for pid in NEW_PARADIGMS:
        data = yaml.safe_load(
            (ASSETS / "teams" / "paradigms" / f"{pid}.yaml").read_text(encoding="utf-8")
        )
        seats = [{"agent_key": m["key"], "role": m.get("role")} for m in data["members"]]
        issues = role_registry_preflight(seats, prompts_dir=ASSETS / "prompts", catalog=catalog)
        assert not issues, f"{pid}: {[v.format() for v in issues]}"


def test_new_roles_in_catalog_with_skills():
    catalog = load_role_catalog()
    for rid in NEW_ROLES:
        assert rid in catalog, rid
        assert catalog[rid].default_skills, f"{rid}: default_skills must not be empty"
        for sid in catalog[rid].default_skills:
            assert (ASSETS / "skills" / sid / "SKILL.md").is_file(), f"{rid} -> {sid}"


def test_skills_exist_and_indexed():
    index_text = (ASSETS / "skills" / "INDEX.md").read_text(encoding="utf-8")
    for sid in NEW_SKILLS:
        assert (ASSETS / "skills" / sid / "SKILL.md").is_file(), sid
        assert sid in index_text, f"{sid} missing from skills INDEX.md"


def test_amend_review_declares_audit_capability():
    text = (ASSETS / "skills" / "org.amend_review" / "SKILL.md").read_text(encoding="utf-8")
    assert "institution.audit" in text
    tools = yaml.safe_load((ASSETS / "tools" / "catalog.yaml").read_text(encoding="utf-8"))
    caps = {t.get("capability") for t in tools.get("tools", [])}
    assert "institution.audit" in caps, "amendment_audit tool must be in the tool catalog"
