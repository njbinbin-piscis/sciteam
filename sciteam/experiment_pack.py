"""Experiment pack: instance copies of agent/skill templates under a run.

System assets under ``assets/`` are templates. Each campaign run keeps its own
editable instances in ``runs/<id>/pack/{agents,skills}/``. Runtime and the
observatory prefer pack instances when present.

HR staffing (``hr.recruit``) is the only institutional path that may create or
retrain agent instances into the pack; mission teams must resolve members from
the pack, not from anonymous generic agents.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from sciteam import asset_paths
from sciteam.roster_policy import (
    load_role_catalog,
    prefer_skills_for_roles,
    validate_roster,
)

# Asset locations resolve through asset_paths (O_ASSETS_ROOT, ENGINE_ISA §2.10)
# so organization instances can swap in their own evolvable asset copies.

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class PackError(ValueError):
    pass


def pack_root(run_dir: Path | str) -> Path:
    return Path(run_dir) / "pack"


def agents_dir(run_dir: Path | str) -> Path:
    return pack_root(run_dir) / "agents"


def skills_dir(run_dir: Path | str) -> Path:
    return pack_root(run_dir) / "skills"


def manifest_path(run_dir: Path | str) -> Path:
    return pack_root(run_dir) / "manifest.json"


def hr_log_path(run_dir: Path | str) -> Path:
    return pack_root(run_dir) / "hr_log.jsonl"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_safe_id(value: str, *, kind: str) -> str:
    vid = str(value or "").strip()
    if not _SAFE_ID.match(vid):
        raise PackError(f"invalid {kind} id: {value!r}")
    return vid


def _write_manifest(run_dir: Path, data: dict[str, Any]) -> None:
    path = manifest_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_manifest(run_dir: Path | str) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def append_hr_log(run_dir: Path | str, event: dict[str, Any]) -> None:
    path = hr_log_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": _now(), **event}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def list_template_skills() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not asset_paths.skills_dir().is_dir():
        return out
    for path in sorted(asset_paths.skills_dir().iterdir()):
        skill_md = path / "SKILL.md"
        if not path.is_dir() or not skill_md.is_file():
            continue
        if path.name.startswith("_"):
            continue
        text = skill_md.read_text(encoding="utf-8")
        out.append(
            {
                "id": path.name,
                "kind": "template",
                "path": asset_paths.display_path(skill_md),
                "chars": len(text),
                "preview": text[:240].replace("\n", " "),
            }
        )
    return out


def list_template_agents() -> list[dict[str, Any]]:
    catalog = load_role_catalog(asset_paths.role_catalog_path())
    out: list[dict[str, Any]] = []
    for rid, spec in sorted(catalog.items()):
        prompt_path = asset_paths.role_prompts_dir() / f"{rid}.md"
        text = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        out.append(
            {
                "id": rid,
                "kind": "template",
                "title": spec.title,
                "summary": spec.summary,
                "default_skills": list(spec.default_skills),
                "preferred_paradigms": list(spec.preferred_paradigms),
                "has_prompt": prompt_path.is_file(),
                "path": asset_paths.display_path(prompt_path) if prompt_path.is_file() else "",
                "chars": len(text),
                "preview": text[:240].replace("\n", " ") if text else spec.summary,
            }
        )
    # Also surface prompt files not yet in catalog
    if asset_paths.role_prompts_dir().is_dir():
        known = {a["id"] for a in out}
        for path in sorted(asset_paths.role_prompts_dir().glob("*.md")):
            rid = path.stem
            if rid in known or rid == "worker":
                continue
            text = path.read_text(encoding="utf-8")
            out.append(
                {
                    "id": rid,
                    "kind": "template",
                    "title": rid,
                    "summary": "",
                    "default_skills": [],
                    "preferred_paradigms": [],
                    "has_prompt": True,
                    "path": asset_paths.display_path(path),
                    "chars": len(text),
                    "preview": text[:240].replace("\n", " "),
                }
            )
    return out


def read_template_skill(skill_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", skill_id):
        raise PackError(f"invalid skill id: {skill_id!r}")
    path = asset_paths.skills_dir() / skill_id / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(skill_id)
    text = path.read_text(encoding="utf-8")
    return {
        "id": skill_id,
        "kind": "template",
        "path": asset_paths.display_path(path),
        "text": text,
    }


def write_template_skill(skill_id: str, text: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", skill_id):
        raise PackError(f"invalid skill id: {skill_id!r}")
    path = asset_paths.skills_dir() / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return read_template_skill(skill_id)


def read_template_agent(agent_id: str) -> dict[str, Any]:
    aid = _ensure_safe_id(agent_id, kind="agent")
    catalog = load_role_catalog(asset_paths.role_catalog_path())
    spec = catalog.get(aid)
    prompt_path = asset_paths.role_prompts_dir() / f"{aid}.md"
    text = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
    return {
        "id": aid,
        "kind": "template",
        "title": spec.title if spec else aid,
        "summary": spec.summary if spec else "",
        "default_skills": list(spec.default_skills) if spec else [],
        "preferred_paradigms": list(spec.preferred_paradigms) if spec else [],
        "path": asset_paths.display_path(prompt_path) if prompt_path.is_file() else "",
        "text": text,
        "catalog_entry": spec is not None,
    }


def write_template_agent(agent_id: str, text: str) -> dict[str, Any]:
    aid = _ensure_safe_id(agent_id, kind="agent")
    path = asset_paths.role_prompts_dir() / f"{aid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return read_template_agent(aid)


def _agent_frontmatter(
    *,
    agent_key: str,
    title: str,
    summary: str,
    skills: list[str],
    trained_from: str,
    template_id: str,
) -> str:
    meta = {
        "id": agent_key,
        "title": title,
        "summary": summary,
        "skills": skills,
        "template_id": template_id,
        "trained_from": trained_from,
        "instance": True,
        "staffed_by": "hr_officer",
    }
    body = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{body}\n---\n"


def parse_agent_md(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, parts[2].lstrip("\n")
    return {}, text


def list_pack_skills(run_dir: Path | str) -> list[dict[str, Any]]:
    root = skills_dir(run_dir)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        skill_md = path / "SKILL.md"
        if not path.is_dir() or not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        out.append(
            {
                "id": path.name,
                "kind": "instance",
                "path": str(skill_md),
                "chars": len(text),
                "preview": text[:240].replace("\n", " "),
            }
        )
    return out


def list_pack_agents(run_dir: Path | str) -> list[dict[str, Any]]:
    root = agents_dir(run_dir)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        agent_md = path / "AGENT.md"
        if not path.is_dir() or not agent_md.is_file():
            continue
        text = agent_md.read_text(encoding="utf-8")
        meta, body = parse_agent_md(text)
        out.append(
            {
                "id": path.name,
                "kind": "instance",
                "title": meta.get("title") or path.name,
                "summary": meta.get("summary") or "",
                "skills": list(meta.get("skills") or []),
                "template_id": meta.get("template_id") or path.name,
                "trained_from": meta.get("trained_from") or "",
                "staffed_by": meta.get("staffed_by") or "",
                "chars": len(text),
                "preview": body[:240].replace("\n", " "),
            }
        )
    return out


def read_pack_skill(run_dir: Path | str, skill_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", skill_id):
        raise PackError(f"invalid skill id: {skill_id!r}")
    path = skills_dir(run_dir) / skill_id / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(skill_id)
    return {
        "id": skill_id,
        "kind": "instance",
        "path": str(path),
        "text": path.read_text(encoding="utf-8"),
    }


def write_pack_skill(run_dir: Path | str, skill_id: str, text: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", skill_id):
        raise PackError(f"invalid skill id: {skill_id!r}")
    path = skills_dir(run_dir) / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    append_hr_log(run_dir, {"action": "edit_skill", "skill_id": skill_id})
    return read_pack_skill(run_dir, skill_id)


def read_pack_agent(run_dir: Path | str, agent_id: str) -> dict[str, Any]:
    aid = _ensure_safe_id(agent_id, kind="agent")
    path = agents_dir(run_dir) / aid / "AGENT.md"
    if not path.is_file():
        raise FileNotFoundError(aid)
    text = path.read_text(encoding="utf-8")
    meta, body = parse_agent_md(text)
    skill_defs = []
    for sid in meta.get("skills") or []:
        try:
            skill_defs.append(read_pack_skill(run_dir, str(sid)))
        except FileNotFoundError:
            try:
                skill_defs.append(read_template_skill(str(sid)))
            except FileNotFoundError:
                skill_defs.append({"id": sid, "kind": "missing", "text": ""})
    return {
        "id": aid,
        "kind": "instance",
        "path": str(path),
        "meta": meta,
        "text": text,
        "body": body,
        "skills": skill_defs,
    }


def write_pack_agent(run_dir: Path | str, agent_id: str, text: str) -> dict[str, Any]:
    aid = _ensure_safe_id(agent_id, kind="agent")
    path = agents_dir(run_dir) / aid / "AGENT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    append_hr_log(run_dir, {"action": "edit_agent", "agent_id": aid})
    return read_pack_agent(run_dir, aid)


def ensure_skills_in_pack(run_dir: Path | str, skill_ids: Iterable[str]) -> list[str]:
    """Copy every allowlisted skill into the run pack or fail closed.

    A skill that is authorized but absent from ``pack/skills/`` is the
    DEF-NEW-16 failure mode: the seat can ``cat`` a path that does not exist.
    """
    copied: list[str] = []
    missing: list[str] = []
    for raw in skill_ids:
        sid = str(raw or "").strip()
        if not sid:
            continue
        try:
            copy_skill_to_pack(run_dir, sid, overwrite=False)
        except (FileNotFoundError, PackError):
            dest = skills_dir(run_dir) / sid / "SKILL.md"
            if dest.is_file():
                copied.append(sid)
                continue
            missing.append(sid)
            continue
        copied.append(sid)
    if missing:
        raise PackError("skills not materialized: " + ", ".join(missing))
    return copied


def copy_skill_to_pack(run_dir: Path | str, skill_id: str, *, overwrite: bool = False) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", skill_id):
        raise PackError(f"invalid skill id: {skill_id!r}")
    src = asset_paths.skills_dir() / skill_id / "SKILL.md"
    if not src.is_file():
        raise FileNotFoundError(f"template skill missing: {skill_id}")
    dest = skills_dir(run_dir) / skill_id / "SKILL.md"
    if dest.is_file() and not overwrite:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def recruit_agent(
    run_dir: Path | str,
    *,
    agent_key: str,
    template_id: str | None = None,
    title: str = "",
    summary: str = "",
    skills: Iterable[str] | None = None,
    train_notes: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """HR action: create/train an agent instance from a template post."""
    key = _ensure_safe_id(agent_key, kind="agent")
    tmpl = _ensure_safe_id(template_id or agent_key, kind="agent")
    catalog = load_role_catalog(asset_paths.role_catalog_path())
    spec = catalog.get(tmpl)
    skill_list = list(skills) if skills is not None else list(spec.default_skills if spec else [])
    if not skill_list:
        skill_list = prefer_skills_for_roles([tmpl], catalog=catalog)

    dest = agents_dir(run_dir) / key / "AGENT.md"
    if dest.is_file() and not overwrite:
        return read_pack_agent(run_dir, key)

    prompt_src = asset_paths.role_prompts_dir() / f"{tmpl}.md"
    base_prompt = (
        prompt_src.read_text(encoding="utf-8")
        if prompt_src.is_file()
        else f"# Role: {key}\n\nSpecialist instance trained for this experiment.\n"
    )
    if train_notes.strip():
        base_prompt = base_prompt.rstrip() + "\n\n## Training notes (this experiment)\n" + train_notes.strip() + "\n"

    header = _agent_frontmatter(
        agent_key=key,
        title=title or (spec.title if spec else key),
        summary=summary or (spec.summary if spec else ""),
        skills=skill_list,
        trained_from=tmpl,
        template_id=tmpl,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(header + "\n" + base_prompt.lstrip(), encoding="utf-8")

    for sid in skill_list:
        try:
            copy_skill_to_pack(run_dir, sid, overwrite=False)
        except FileNotFoundError:
            continue

    append_hr_log(
        run_dir,
        {
            "action": "recruit",
            "agent_id": key,
            "template_id": tmpl,
            "skills": skill_list,
        },
    )
    return read_pack_agent(run_dir, key)


def materialize_pack(
    run_dir: Path | str,
    *,
    role_ids: Iterable[str] | None = None,
    skill_ids: Iterable[str] | None = None,
    include_hr: bool = True,
) -> dict[str, Any]:
    """Seed an experiment pack from templates (HR desks + requested posts)."""
    run_dir = Path(run_dir)
    pack_root(run_dir).mkdir(parents=True, exist_ok=True)
    catalog = load_role_catalog(asset_paths.role_catalog_path())
    # Default: only seat HR; mission staffing recruits the rest on demand.
    roles = list(role_ids) if role_ids is not None else ["hr_officer"]
    if include_hr and "hr_officer" not in roles:
        roles = ["hr_officer", *roles]

    agents_created: list[str] = []
    for rid in roles:
        if rid == "worker" or not _SAFE_ID.match(rid):
            continue
        recruit_agent(run_dir, agent_key=rid, template_id=rid, overwrite=False)
        agents_created.append(rid)

    skills = set(skill_ids or [])
    for rid in agents_created:
        skills.update(prefer_skills_for_roles([rid], catalog=catalog))
    skills.add("hr.recruit")
    skill_copied: list[str] = []
    for sid in sorted(skills):
        try:
            copy_skill_to_pack(run_dir, sid, overwrite=False)
            skill_copied.append(sid)
        except FileNotFoundError:
            continue

    man = {
        "version": 1,
        "created_at": _now(),
        "updated_at": _now(),
        "template_skills_root": asset_paths.display_path(asset_paths.skills_dir()),
        "template_agents_root": asset_paths.display_path(asset_paths.role_prompts_dir()),
        "agents": agents_created,
        "skills": skill_copied,
        "staffing_policy": "all_rosters_via_hr_officer",
    }
    _write_manifest(run_dir, man)
    append_hr_log(run_dir, {"action": "materialize", "agents": agents_created, "skills": skill_copied})
    return man


@dataclass(frozen=True)
class StaffedMember:
    agent_key: str
    agent_id: str
    name: str
    role: str
    skills: tuple[str, ...]


def staff_roster_via_hr(
    run_dir: Path | str,
    template_members: list[dict[str, Any]],
    *,
    mission_id: str = "",
) -> list[dict[str, Any]]:
    """Institutional gate: mission roster must be pack instances staffed by HR.

    Missing instances are recruited from matching templates (hr.recruit).
    """
    run_dir = Path(run_dir)
    if not pack_root(run_dir).is_dir():
        materialize_pack(run_dir, role_ids=[m.get("agent_key") or m.get("key") for m in template_members])

    # Ensure HR officer exists
    if not (agents_dir(run_dir) / "hr_officer" / "AGENT.md").is_file():
        recruit_agent(run_dir, agent_key="hr_officer", template_id="hr_officer")

    staffed: list[dict[str, Any]] = []
    for item in template_members:
        key = str(item.get("agent_key") or item.get("key") or "").strip()
        if not key:
            continue
        role = str(item.get("role") or key)
        tmpl = role if (asset_paths.role_prompts_dir() / f"{role}.md").is_file() else key
        if not (agents_dir(run_dir) / key / "AGENT.md").is_file():
            recruit_agent(
                run_dir,
                agent_key=key,
                template_id=tmpl,
                title=str(item.get("name") or key),
            )
        detail = read_pack_agent(run_dir, key)
        meta = detail.get("meta") or {}
        member_skills = [str(s) for s in (meta.get("skills") or item.get("skills") or [])]
        for sid in member_skills:
            try:
                copy_skill_to_pack(run_dir, sid, overwrite=False)
            except (FileNotFoundError, PackError):
                continue
        staffed.append(
            {
                "agent_key": key,
                "agent_id": key,
                "name": str(meta.get("title") or item.get("name") or key),
                "role": role,
                "skills": list(meta.get("skills") or []),
                "staffed_by": "hr_officer",
                "template_id": meta.get("template_id") or tmpl,
            }
        )
    violations = validate_roster(staffed)
    if violations:
        raise PackError("; ".join(v.format() for v in violations))
    append_hr_log(
        run_dir,
        {
            "action": "staff_mission",
            "mission_id": mission_id,
            "agents": [a["agent_key"] for a in staffed],
        },
    )
    return staffed


def pack_overview(run_dir: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir)
    return {
        "has_pack": pack_root(run_dir).is_dir(),
        "manifest": read_manifest(run_dir),
        "agents": list_pack_agents(run_dir),
        "skills": list_pack_skills(run_dir),
        "hr_log_tail": _tail_hr_log(run_dir, n=30),
    }


def _tail_hr_log(run_dir: Path | str, n: int = 30) -> list[dict[str, Any]]:
    path = hr_log_path(run_dir)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
