"""Official Docker container sandbox plugin for SciTeam."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sciteam.plugin_api import (
    PLUGIN_API_VERSION,
    HookResult,
    PluginAPI,
    PluginManifest,
)
from sciteam.sandbox import SandboxRequest
from sciteam.tool_catalog import ToolSpec

from sciteam_sandbox_docker.guard import ContainerGuardSpec
from sciteam_sandbox_docker.sandbox import DockerSandbox

logger = logging.getLogger(__name__)

__all__ = [
    "ContainerGuardSpec",
    "DockerSandbox",
    "register",
]


def _docker_sandbox_run_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a task in a container sandbox."""
    workspace = Path(arguments.get("workspace", ".")).resolve()
    guard_data = arguments.get("guard", {})
    guard = ContainerGuardSpec.from_dict(guard_data) if guard_data else None

    sandbox = DockerSandbox(workspace=workspace, guard=guard)
    req = SandboxRequest(
        program=str(arguments.get("program", "")),
        inputs=tuple(str(x) for x in (arguments.get("inputs") or ())),
        outputs=tuple(str(x) for x in (arguments.get("outputs") or ())),
        arguments=tuple(str(x) for x in (arguments.get("arguments") or ())),
        timeout_seconds=int(arguments.get("timeout_seconds") or 30),
        memory_mb=int(arguments.get("memory_mb") or 512),
    )
    result = sandbox.run(req)
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "output_hashes": result.output_hashes,
        "environment_fingerprint": result.environment_fingerprint,
        "reason_code": result.reason_code,
    }


def _guard_tool_call_hook(payload: dict[str, Any]) -> HookResult | None:
    """Audit tool calls requesting container sandbox execution."""
    tool_name = payload.get("tool_name", "")
    if tool_name == "docker_sandbox_run":
        args = payload.get("arguments", {})
        program = args.get("program", "")
        if ".." in program or program.startswith("/"):
            return HookResult(
                block=True,
                reason="Path traversal or absolute path detected in container sandbox program",
            )
    return None


def register(api: PluginAPI) -> PluginManifest:
    """Register the Docker sandbox plugin into SciTeam."""
    # 1. Register compute port
    api.register_port(
        "compute",
        "docker",
        lambda: DockerSandbox(workspace=Path.cwd()),
    )

    # 2. Register tool
    api.register_tool(
        ToolSpec(
            name="docker_sandbox_run",
            version="1.0",
            capability="compute.sandbox.docker",
            summary="Execute a program within an isolated, hermetic Docker container.",
            permission="side_effect",
            input_schema={
                "type": "object",
                "properties": {
                    "program": {
                        "type": "string",
                        "description": "Program path relative to workspace",
                    },
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "default": 30},
                    "memory_mb": {"type": "integer", "default": 512},
                },
                "required": ["program"],
            },
        ),
        handler=_docker_sandbox_run_handler,
    )

    # 3. Register pre-execution hook
    api.on("tool_call", _guard_tool_call_hook)

    return PluginManifest(
        id="sciteam-sandbox-docker",
        version="0.1.0",
        sciteam_api_version=PLUGIN_API_VERSION,
        provides=("compute", "tool", "hook"),
        description="Official Docker container sandbox plugin for SciTeam",
    )
