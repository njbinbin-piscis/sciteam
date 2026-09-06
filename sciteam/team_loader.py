"""YAML team templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sciteam.coordination import CoordinationSpec, StoppingKind
from sciteam.roster_policy import (
    load_role_catalog,
    roster_policy_enabled,
    validate_roster,
)


@dataclass(frozen=True)
class MemberSpec:
    agent_key: str
    agent_id: str
    name: str = ""
    role: str = ""
    # When true, this seat may write the mission exit-contract artifact.
    emits_exit_artifact: bool = False
    authority: tuple[str, ...] = ()
    task_brief: str = ""

    @property
    def may_assess_round(self) -> bool:
        return "may_assess_round" in self.authority


@dataclass(frozen=True)
class TeamConfig:
    team_id: str
    name: str
    description: str
    members: list[MemberSpec]
    coordination: dict[str, Any] = field(default_factory=dict)

    def agent_snapshots(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in self.members:
            item = {
                "agent_key": m.agent_key,
                "agent_id": m.agent_id,
                "name": m.name or m.agent_key,
            }
            if m.role:
                item["role"] = m.role
            else:
                item["role"] = m.agent_key
            if m.emits_exit_artifact:
                item["emits_exit_artifact"] = True
            if m.authority:
                item["authority"] = list(m.authority)
            if m.may_assess_round:
                item["may_assess_round"] = True
            if m.task_brief:
                item["task_brief"] = m.task_brief
            out.append(item)
        return out

    def artifact_emit_roles(self) -> list[str]:
        """Roles/keys allowed to write the exit artifact for this paradigm.

        Sole source: member seats marked ``emits_exit_artifact: true`` in YAML.
        """
        roles: list[str] = []
        for m in self.members:
            if not m.emits_exit_artifact:
                continue
            for token in (m.role, m.agent_key):
                t = str(token or "").strip()
                if t and t not in roles:
                    roles.append(t)
        return roles

    def assess_roles(self) -> list[str]:
        """Roles/keys allowed to author round_assessment verdicts."""
        roles: list[str] = []
        for m in self.members:
            if not m.may_assess_round:
                continue
            for token in (m.role, m.agent_key):
                t = str(token or "").strip()
                if t and t not in roles:
                    roles.append(t)
        return roles

    def may_veto_roles(self) -> list[str]:
        """Roles/keys authorized to issue an audit veto (I_PROCESS_AUDIT).

        Sole source: member seats carrying ``may_veto_exit`` in ``authority``
        (declared via ``authority: [may_veto_exit]`` or the boolean shorthand
        ``may_veto_exit: true`` — see ``_parse_authority``). A veto signal
        (``sciteam/audit_veto.py``) from any seat not in this list is scanned
        but never honored — the same fail-closed pattern as
        ``artifact_emit_roles``/``assess_roles``.
        """
        roles: list[str] = []
        for m in self.members:
            if "may_veto_exit" not in m.authority:
                continue
            for token in (m.role, m.agent_key):
                t = str(token or "").strip()
                if t and t not in roles:
                    roles.append(t)
        return roles


class TeamRegistry:
    def __init__(self, teams: list[TeamConfig] | None = None) -> None:
        self._teams: dict[str, TeamConfig] = {}
        for team in teams or []:
            self.register(team)

    def register(self, team: TeamConfig) -> None:
        self._teams[team.team_id] = team

    def get(self, team_id: str) -> TeamConfig | None:
        return self._teams.get(team_id)

    def list(self) -> list[TeamConfig]:
        return list(self._teams.values())


def _parse_authority(item: dict[str, Any]) -> tuple[str, ...]:
    auth: list[str] = []
    raw = item.get("authority")
    if isinstance(raw, str) and raw.strip():
        auth.append(raw.strip())
    elif isinstance(raw, (list, tuple)):
        for x in raw:
            t = str(x or "").strip()
            if t and t not in auth:
                auth.append(t)
    if item.get("may_assess_round") is True and "may_assess_round" not in auth:
        auth.append("may_assess_round")
    if item.get("may_veto_exit") is True and "may_veto_exit" not in auth:
        auth.append("may_veto_exit")
    return tuple(auth)


def load_team_config(path: Path) -> TeamConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: team config must be a mapping")
    team_id = str(raw.get("id") or path.stem)
    members_raw = raw.get("members") or []
    if not isinstance(members_raw, list) or not members_raw:
        raise ValueError(f"{path}: members must be a non-empty list")
    members: list[MemberSpec] = []
    for index, item in enumerate(members_raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: members[{index}] must be a mapping")
        agent_key = str(item.get("key") or item.get("agent_key") or "")
        agent_id = str(item.get("agent_id") or agent_key)
        if not agent_key:
            raise ValueError(f"{path}: members[{index}] missing key")
        emits = item.get("emits_exit_artifact")
        if emits is None:
            emits = item.get("artifact_duty")
        authority = _parse_authority(item)
        if bool(emits) and "may_assess_round" in authority:
            raise ValueError(
                f"{path}: members[{index}] ({agent_key}) cannot both "
                "emits_exit_artifact and may_assess_round"
            )
        members.append(
            MemberSpec(
                agent_key=agent_key,
                agent_id=agent_id,
                name=str(item.get("name") or agent_key),
                role=str(item.get("role") or agent_key),
                emits_exit_artifact=bool(emits),
                authority=authority,
                task_brief=str(item.get("task_brief") or "").strip(),
            )
        )
    if roster_policy_enabled():
        snapshots = [
            {
                "agent_key": m.agent_key,
                "agent_id": m.agent_id,
                "name": m.name,
                "role": m.role,
            }
            for m in members
        ]
        violations = validate_roster(snapshots, catalog=load_role_catalog())
        if violations:
            detail = "; ".join(v.format() for v in violations)
            raise ValueError(f"{path}: roster policy violated — {detail}")
    coordination_raw = raw.get("coordination") or {}
    if not isinstance(coordination_raw, dict):
        raise ValueError(f"{path}: coordination must be a mapping")
    coord = CoordinationSpec.from_dict(coordination_raw)
    assess = [m for m in members if m.may_assess_round]
    if coord.stopping.kind == StoppingKind.JUDGMENT and not assess:
        raise ValueError(
            f"{path}: stopping.kind=judgment requires at least one member "
            "with may_assess_round / authority including may_assess_round"
        )
    return TeamConfig(
        team_id=team_id,
        name=str(raw.get("name") or team_id),
        description=str(raw.get("description") or ""),
        members=members,
        coordination=dict(coordination_raw),
    )


def load_team_configs(root: Path | str) -> list[TeamConfig]:
    base = Path(root)
    if not base.is_dir():
        return []
    return [load_team_config(p) for p in sorted(base.glob("*.yaml"))]


def build_registry(root: Path | str) -> TeamRegistry:
    return TeamRegistry(load_team_configs(root))


def resolve_start_params(
    team_id: str,
    *,
    registry: TeamRegistry,
    agents: list[dict[str, Any]] | None = None,
    coordination: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    cfg = registry.get(team_id)
    if cfg is None:
        raise KeyError(f"unknown team template: {team_id}")
    resolved_agents = agents if agents is not None else cfg.agent_snapshots()
    # Expand replicas
    spec = CoordinationSpec.from_dict(
        coordination if coordination is not None else cfg.coordination
    )
    if spec.replicas:
        expanded: list[dict[str, Any]] = []
        for agent in resolved_agents:
            key = str(agent.get("agent_key") or "")
            count = spec.replicas.get(key, 1)
            if count <= 1:
                expanded.append(dict(agent))
                continue
            for i in range(1, count + 1):
                clone = dict(agent)
                clone["agent_key"] = f"{key}_{i}"
                clone["name"] = f"{agent.get('name') or key} #{i}"
                expanded.append(clone)
        resolved_agents = expanded
    coord = coordination if coordination is not None else dict(cfg.coordination)
    return resolved_agents, coord
