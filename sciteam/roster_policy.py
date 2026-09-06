"""Roster naming and professionalism policy.

Team composition must use task-fit specialist posts — never a generic agent
identity, and never meaningless reusable labels like stage_a / stage_b.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sciteam import asset_paths

_BANNED_EXACT = frozenset(
    {
        "worker",
        "generic",
        "agent",
        "solo",
        "default",
        "member",
        "stage",
        "stage_a",
        "stage_b",
        "stage_c",
        "stage_d",
        "stage-a",
        "stage-b",
        "stage-c",
        "stage-d",
    }
)
_BANNED_KEY_RE = re.compile(r"(?i)^(stage[-_]?[a-z0-9]+|worker\d*|agent\d*|generic([-_].*)?)$")
_PROFESSIONAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


@dataclass(frozen=True)
class RoleSpec:
    id: str
    title: str
    summary: str
    default_skills: tuple[str, ...]
    preferred_paradigms: tuple[str, ...]


@dataclass(frozen=True)
class RosterViolation:
    field: str
    value: str
    detail: str

    def format(self) -> str:
        return f"{self.field}={self.value!r}: {self.detail}"


def load_role_catalog(path: Path | str | None = None) -> dict[str, RoleSpec]:
    if path is None:
        path = asset_paths.role_catalog_path()
    path = Path(path)
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, RoleSpec] = {}
    for item in data.get("roles") or []:
        rid = str(item.get("id") or "").strip()
        if not rid:
            continue
        out[rid] = RoleSpec(
            id=rid,
            title=str(item.get("title") or rid),
            summary=str(item.get("summary") or ""),
            default_skills=tuple(str(s) for s in (item.get("default_skills") or [])),
            preferred_paradigms=tuple(str(s) for s in (item.get("preferred_paradigms") or [])),
        )
    return out


def _norm(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip().lower())


def is_banned_identity(value: str) -> bool:
    n = _norm(value)
    if n in _BANNED_EXACT:
        return True
    return bool(_BANNED_KEY_RE.match(n))


def validate_agent_identity(
    *,
    agent_key: str,
    name: str = "",
    agent_id: str = "",
    role: str = "",
    catalog: dict[str, RoleSpec] | None = None,
    require_catalog: bool = False,
) -> list[RosterViolation]:
    """Validate one roster member. Empty key is always an error."""
    issues: list[RosterViolation] = []
    key = str(agent_key or "").strip()
    if not key:
        issues.append(RosterViolation("agent_key", "", "missing professional post id"))
        return issues
    if is_banned_identity(key):
        issues.append(
            RosterViolation(
                "agent_key",
                key,
                "generic or stage-* names are forbidden; use a catalog post "
                "(e.g. literature_lead, implementer, epistemic_reviewer)",
            )
        )
    elif not _PROFESSIONAL_KEY_RE.match(key):
        issues.append(
            RosterViolation(
                "agent_key",
                key,
                "must be snake_case professional id (letters/digits/underscore)",
            )
        )

    for field, value in (
        ("name", name),
        ("agent_id", agent_id),
        ("role", role),
    ):
        if not value:
            continue
        if is_banned_identity(str(value)):
            issues.append(
                RosterViolation(
                    field,
                    str(value),
                    "must not be a generic/stage identity",
                )
            )

    cat = catalog if catalog is not None else {}
    if require_catalog and cat and key not in cat and not issues:
        # Allow task-specific new posts, but require they look professional
        # and are not banned — warn only when require_catalog and unknown.
        issues.append(
            RosterViolation(
                "agent_key",
                key,
                "not in role catalog; create assets/roles entry or pick a listed post",
            )
        )
    return issues


def validate_roster(
    agents: Iterable[dict[str, Any]],
    *,
    catalog: dict[str, RoleSpec] | None = None,
    require_catalog: bool = False,
) -> list[RosterViolation]:
    issues: list[RosterViolation] = []
    seen: set[str] = set()
    for item in agents:
        key = str(item.get("agent_key") or item.get("key") or "").strip()
        if key in seen:
            issues.append(RosterViolation("agent_key", key, "duplicate post id in roster"))
        seen.add(key)
        issues.extend(
            validate_agent_identity(
                agent_key=key,
                name=str(item.get("name") or ""),
                agent_id=str(item.get("agent_id") or ""),
                role=str(item.get("role") or item.get("profile", {}).get("role") or "")
                if isinstance(item.get("profile"), dict)
                else str(item.get("role") or ""),
                catalog=catalog,
                require_catalog=require_catalog,
            )
        )
    return issues


def roster_policy_enabled() -> bool:
    """Tests may set SCITEAM_ROSTER_RELAX=1 to skip enforcement."""
    return os.environ.get("SCITEAM_ROSTER_RELAX", "").strip() not in {"1", "true", "yes"}


_REPLICA_SUFFIX_RE = re.compile(r"(.+)_(\d+)$")


def _prompt_role_key(agent_key: str) -> str:
    """Mirror ``sciteam.llm_worker._role_key``: strip a replica suffix
    (``debater_2`` -> ``debater``) so the prompt-file check looks up the same
    stem the runtime will actually resolve at dispatch time. Duplicated here
    rather than imported to keep this module (loaded at mission-preflight
    time, before any runtime is constructed) free of the LLM worker's
    heavier import surface.
    """
    match = _REPLICA_SUFFIX_RE.fullmatch(agent_key)
    return match.group(1) if match else agent_key


def role_registry_preflight(
    agents: Iterable[dict[str, Any]],
    *,
    prompts_dir: Path,
    catalog: dict[str, RoleSpec] | None = None,
) -> list[RosterViolation]:
    """Fail-closed check: every declared seat must resolve to *both* a
    catalog role spec (for default_skills) and a dedicated prompt file (for
    role-specific instructions) — never a silent ``worker.md`` fallback.

    Runs unconditionally, independent of ``roster_policy_enabled()`` /
    ``SCITEAM_ROSTER_RELAX`` (that flag only relaxes naming *style*, not
    whether a seat has any training at all). This closes the fail-open gap
    that let an unregistered ``reviewer`` seat run with ``skills: []`` and
    the generic worker prompt, then fabricate citations with no role-
    specific discipline (A3 in the institution-repair plan).
    """
    cat = catalog if catalog is not None else load_role_catalog()
    issues: list[RosterViolation] = []
    for item in agents:
        agent_key = str(item.get("agent_key") or item.get("key") or "").strip()
        if not agent_key:
            continue
        role = str(item.get("role") or agent_key).strip()
        has_catalog = role in cat
        prompt_path = prompts_dir / "roles" / f"{_prompt_role_key(agent_key)}.md"
        has_prompt = prompt_path.is_file()
        if has_catalog and has_prompt:
            continue
        missing = []
        if not has_catalog:
            missing.append(f"no CATALOG.yaml entry for role={role!r}")
        if not has_prompt:
            missing.append(
                f"no assets/prompts/roles/{_prompt_role_key(agent_key)}.md "
                "(would silently fall back to worker.md)"
            )
        issues.append(RosterViolation("agent_key", agent_key, "; ".join(missing)))
    return issues


def prefer_skills_for_roles(
    role_ids: Iterable[str],
    *,
    catalog: dict[str, RoleSpec] | None = None,
) -> list[str]:
    """Union default_skills for selected roles (stable order)."""
    cat = catalog if catalog is not None else load_role_catalog()
    out: list[str] = []
    seen: set[str] = set()
    for rid in role_ids:
        spec = cat.get(str(rid))
        if spec is None:
            continue
        for skill in spec.default_skills:
            if skill not in seen:
                seen.add(skill)
                out.append(skill)
    return out
