"""docs/28 阶段4 — unattended profile forces Docker, never silently degrades.

`_sandbox_handler` (the `compute.sandbox`/`model.certify` tool dispatch) must
fail closed when the product-default `unattended` profile cannot obtain a
Docker-backed sandbox, and must never fall back to the uncontained
`LocalPythonSandbox` on its own initiative.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest
from sciteam import wizard_tools
from sciteam.sandbox import (
    DEFAULT_SANDBOX_PROFILE,
    DEVELOPMENT_SANDBOX_PROFILE,
    UNATTENDED_SANDBOX_PROFILE,
    LocalPythonSandbox,
    SandboxUnavailableError,
    build_sandbox,
    resolve_sandbox_profile,
)


def test_default_profile_is_unattended():
    assert DEFAULT_SANDBOX_PROFILE == UNATTENDED_SANDBOX_PROFILE
    assert resolve_sandbox_profile(None) == UNATTENDED_SANDBOX_PROFILE


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("SCITEAM_SANDBOX_PROFILE", DEVELOPMENT_SANDBOX_PROFILE)
    assert resolve_sandbox_profile(None) == DEVELOPMENT_SANDBOX_PROFILE


def test_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("SCITEAM_SANDBOX_PROFILE", DEVELOPMENT_SANDBOX_PROFILE)
    assert resolve_sandbox_profile(UNATTENDED_SANDBOX_PROFILE) == UNATTENDED_SANDBOX_PROFILE


def test_development_profile_builds_local_sandbox(tmp_path: Path):
    sandbox = build_sandbox(tmp_path, profile=DEVELOPMENT_SANDBOX_PROFILE)
    assert isinstance(sandbox, LocalPythonSandbox)


def test_unattended_without_docker_plugin_fails_closed(tmp_path: Path, monkeypatch):
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "sciteam_sandbox_docker" or name.startswith("sciteam_sandbox_docker."):
            raise ImportError("simulated: plugin not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    with pytest.raises(SandboxUnavailableError):
        build_sandbox(tmp_path, profile=UNATTENDED_SANDBOX_PROFILE)


def test_unattended_never_silently_returns_local_sandbox(tmp_path: Path, monkeypatch):
    """Even when Docker itself is unreachable (not just the plugin missing),
    `build_sandbox` must not quietly hand back a `LocalPythonSandbox` —
    the plugin's own `DockerSandbox.run()` fails closed with a reason_code
    instead, which is asserted at the handler level below."""
    sandbox = build_sandbox(tmp_path, profile=UNATTENDED_SANDBOX_PROFILE)
    assert not isinstance(sandbox, LocalPythonSandbox)


def test_unknown_profile_rejected(tmp_path: Path):
    with pytest.raises(SandboxUnavailableError):
        build_sandbox(tmp_path, profile="production-yolo")


def test_sandbox_handler_defaults_to_unattended_and_refuses_without_docker(
    tmp_path: Path, monkeypatch
):
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "sciteam_sandbox_docker":
            raise ImportError("simulated: plugin not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    result = wizard_tools._sandbox_handler(
        {"program": "model.py"},
        {"mission_workspace": str(tmp_path)},
        certify=False,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "sandbox_unavailable"


def test_sandbox_handler_development_profile_runs_locally(tmp_path: Path):
    (tmp_path / "model.py").write_text(
        "import json\njson.dump({'ok': True}, open('out.json', 'w'))\n",
        encoding="utf-8",
    )
    result = wizard_tools._sandbox_handler(
        {"program": "model.py", "outputs": ["out.json"]},
        {"mission_workspace": str(tmp_path), "sandbox_profile": DEVELOPMENT_SANDBOX_PROFILE},
        certify=False,
    )
    assert result["ok"] is True


def test_sandbox_handler_missing_workspace_is_unaffected_by_profile():
    result = wizard_tools._sandbox_handler(
        {"program": "model.py"},
        {},
        certify=False,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "sandbox_workspace_missing"
