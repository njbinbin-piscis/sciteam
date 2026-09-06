"""Member authority / judgment assess seat loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sciteam.team_loader import load_team_config


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "team.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def test_judgment_requires_assess_seat(tmp_path, monkeypatch):
    monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
    path = _write(
        tmp_path,
        {
            "id": "no_assess",
            "members": [
                {"key": "w", "agent_id": "w", "role": "writer", "emits_exit_artifact": True}
            ],
            "coordination": {"stopping": {"kind": "judgment", "max_iterations": 2}},
        },
    )
    with pytest.raises(ValueError, match="may_assess_round"):
        load_team_config(path)


def test_emit_and_assess_mutex(tmp_path, monkeypatch):
    monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
    path = _write(
        tmp_path,
        {
            "id": "bad",
            "members": [
                {
                    "key": "w",
                    "agent_id": "w",
                    "role": "writer",
                    "emits_exit_artifact": True,
                    "authority": ["may_assess_round"],
                }
            ],
            "coordination": {"stopping": {"kind": "judgment"}},
        },
    )
    with pytest.raises(ValueError, match="cannot both"):
        load_team_config(path)


def test_judgment_with_assess_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
    path = _write(
        tmp_path,
        {
            "id": "ok",
            "members": [
                {"key": "w", "agent_id": "w", "role": "writer", "emits_exit_artifact": True},
                {
                    "key": "a",
                    "agent_id": "a",
                    "role": "round_assessor",
                    "authority": ["may_assess_round"],
                },
            ],
            "coordination": {"stopping": {"kind": "judgment", "max_iterations": 2}},
        },
    )
    cfg = load_team_config(path)
    assert cfg.assess_roles() == ["round_assessor", "a"] or "a" in cfg.assess_roles()
    assert "writer" in cfg.artifact_emit_roles() or "w" in cfg.artifact_emit_roles()


def test_may_veto_roles(tmp_path, monkeypatch):
    """`may_veto_exit` (line A close-loop gap #2): I_PROCESS_AUDIT authority
    declaration consumed by `sciteam.audit_veto.VetoUpstreamReviser`."""
    monkeypatch.setenv("SCITEAM_ROSTER_RELAX", "1")
    path = _write(
        tmp_path,
        {
            "id": "with_auditor",
            "members": [
                {"key": "w", "agent_id": "w", "role": "writer", "emits_exit_artifact": True},
                {
                    "key": "auditor",
                    "agent_id": "auditor",
                    "role": "process_auditor",
                    "authority": ["may_veto_exit"],
                },
            ],
            "coordination": {"stopping": {"kind": "verification"}},
        },
    )
    cfg = load_team_config(path)
    assert "process_auditor" in cfg.may_veto_roles() or "auditor" in cfg.may_veto_roles()
    assert "w" not in cfg.may_veto_roles() and "writer" not in cfg.may_veto_roles()
