"""Tests for sciteam-sandbox-docker plugin."""

from __future__ import annotations

import json
from pathlib import Path

from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginRegistry
from sciteam.sandbox import SandboxRequest
from sciteam_sandbox_docker import (
    ContainerGuardSpec,
    DockerSandbox,
    register,
)


def test_register_manifest_and_components() -> None:
    registry = PluginRegistry()
    api = PluginAPI("sandbox_test", registry)
    manifest = register(api)

    assert manifest.id == "sciteam-sandbox-docker"
    assert manifest.version == "0.1.0"
    assert manifest.sciteam_api_version == PLUGIN_API_VERSION
    assert "compute" in manifest.provides
    assert "tool" in manifest.provides
    assert "hook" in manifest.provides

    # Port factory
    factory = registry.port_factory("compute", "docker")
    assert factory is not None
    port = factory()
    assert isinstance(port, DockerSandbox)

    # Tool spec
    spec = registry.tool_spec("docker_sandbox_run")
    assert spec is not None
    assert spec.permission == "side_effect"
    assert spec.capability == "compute.sandbox.docker"


def test_guard_spec_from_dict() -> None:
    guard = ContainerGuardSpec.from_dict(
        {
            "image": "my-science-image:v1",
            "network": "bridge",
            "memory_mb": 1024,
            "cpus": 2.5,
            "pids_limit": 128,
            "read_only_root": False,
            "timeout_seconds": 120,
        }
    )
    assert guard.image == "my-science-image:v1"
    assert guard.network == "bridge"
    assert guard.memory_mb == 1024
    assert guard.cpus == 2.5
    assert guard.pids_limit == 128
    assert guard.read_only_root is False
    assert guard.timeout_seconds == 120


def test_docker_command_construction(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("print('hello')", encoding="utf-8")
    guard = ContainerGuardSpec(
        image="python:3.11-alpine",
        network="none",
        memory_mb=256,
        cpus=1.5,
        pids_limit=32,
        read_only_root=True,
    )
    sandbox = DockerSandbox(tmp_path, guard=guard)
    req = SandboxRequest(
        program="script.py",
        arguments=("--test", "arg1"),
    )
    cmd = sandbox.build_docker_command(req)

    assert cmd[0] == "docker"
    assert "run" in cmd
    assert "--rm" in cmd
    assert "--network=none" in cmd
    assert "--memory=256m" in cmd
    assert "--cpus=1.5" in cmd
    assert "--pids-limit=32" in cmd
    assert "--read-only" in cmd
    assert f"{tmp_path}:/workspace:rw" in cmd
    assert cmd[-5:] == ["python", "-I", "/workspace/script.py", "--test", "arg1"]


def test_sandbox_path_escape_denied(tmp_path: Path) -> None:
    sandbox = DockerSandbox(tmp_path)
    res = sandbox.run(SandboxRequest(program="../outside.py"))
    assert res.ok is False
    assert res.reason_code == "sandbox_path_denied"


def test_sandbox_run_and_certify_with_mock_runner(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("dummy", encoding="utf-8")
    (tmp_path / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    captured_cmds: list[list[str]] = []

    def mock_runner(cmd: list[str], timeout: int) -> tuple[int, bytes, bytes, bool]:
        captured_cmds.append(cmd)
        # simulate program writing result.json
        (tmp_path / "result.json").write_text(json.dumps({"sum": 3}), encoding="utf-8")
        return (0, b"Calculation completed successfully\n", b"", False)

    sandbox = DockerSandbox(tmp_path, runner=mock_runner)
    req = SandboxRequest(
        program="calc.py",
        inputs=("input.csv",),
        outputs=("result.json",),
        arguments=("--mode", "eval"),
    )

    res = sandbox.run(req)
    assert res.ok is True
    assert res.exit_code == 0
    assert "Calculation completed" in res.stdout
    assert "result.json" in res.output_hashes
    assert len(res.output_hashes["result.json"]) == 64  # valid sha256
    assert res.environment_fingerprint != ""

    # certify delegates to run
    cert = sandbox.certify(req)
    assert cert.ok is True
    assert cert.output_hashes == res.output_hashes
    assert len(captured_cmds) == 2


def test_sandbox_timeout_handling(tmp_path: Path) -> None:
    (tmp_path / "hang.py").write_text("dummy", encoding="utf-8")

    def timeout_runner(cmd: list[str], timeout: int) -> tuple[int, bytes, bytes, bool]:
        return (None, b"part of stdout", b"process timed out after 5s", True)

    sandbox = DockerSandbox(tmp_path, runner=timeout_runner)
    res = sandbox.run(SandboxRequest(program="hang.py", timeout_seconds=5))
    assert res.ok is False
    assert res.timed_out is True
    assert res.reason_code == "sandbox_timeout"
    assert "process timed out" in res.stderr


def test_tool_call_hook_blocks_traversal_attempts() -> None:
    registry = PluginRegistry()
    api = PluginAPI("docker_hook_test", registry)
    register(api)

    hooks = registry._hooks.get("tool_call", [])
    assert len(hooks) == 1
    _, hook_fn = hooks[0]

    # Valid relative program passes
    assert hook_fn({"tool_name": "docker_sandbox_run", "arguments": {"program": "eval.py"}}) is None

    # Escaping relative path blocked
    payload_rel = {"tool_name": "docker_sandbox_run", "arguments": {"program": "../escape.py"}}
    blocked_rel = hook_fn(payload_rel)
    assert blocked_rel is not None
    assert blocked_rel.block is True
    assert "Path traversal" in blocked_rel.reason

    # Absolute path blocked
    payload_abs = {"tool_name": "docker_sandbox_run", "arguments": {"program": "/etc/passwd"}}
    blocked_abs = hook_fn(payload_abs)
    assert blocked_abs is not None
    assert blocked_abs.block is True
    assert "absolute path" in blocked_abs.reason
