"""Integration tests for official SciTeam plugin matrix.

Verifies that sciteam-runtime-anthropic, sciteam-runtime-deepseek, and
sciteam-tool-mcp can co-exist within a single composition root, register
without conflicts, and adhere to frozen plugin contracts.
"""

from __future__ import annotations

import pytest
from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginRegistry

sciteam_runtime_anthropic = pytest.importorskip("sciteam_runtime_anthropic")
sciteam_runtime_deepseek = pytest.importorskip("sciteam_runtime_deepseek")
sciteam_tool_mcp = pytest.importorskip("sciteam_tool_mcp")
sciteam_sandbox_docker = pytest.importorskip("sciteam_sandbox_docker")


def test_official_plugin_matrix_registration_and_no_conflict() -> None:
    registry = PluginRegistry()

    # Register official plugins sequentially as a composition root would
    manifest_ant = sciteam_runtime_anthropic.register(PluginAPI("runtime_anthropic", registry))
    manifest_ds = sciteam_runtime_deepseek.register(PluginAPI("runtime_deepseek", registry))
    manifest_mcp = sciteam_tool_mcp.register(PluginAPI("tool_mcp", registry))
    manifest_docker = sciteam_sandbox_docker.register(PluginAPI("sandbox_docker", registry))

    # 1. Assert manifests
    assert manifest_ant.id == "sciteam-runtime-anthropic"
    assert manifest_ant.sciteam_api_version == PLUGIN_API_VERSION

    assert manifest_ds.id == "sciteam-runtime-deepseek"
    assert manifest_ds.sciteam_api_version == PLUGIN_API_VERSION

    assert manifest_mcp.id == "sciteam-tool-mcp"
    assert manifest_mcp.sciteam_api_version == PLUGIN_API_VERSION

    assert manifest_docker.id == "sciteam-sandbox-docker"
    assert manifest_docker.sciteam_api_version == PLUGIN_API_VERSION
    assert "compute" in manifest_docker.provides

    # 2. Assert runtime ports
    ant_factory = registry.runtime_port_factory("anthropic")
    assert ant_factory is not None
    ant_port = ant_factory()
    assert ant_port.version().startswith("anthropic:")

    ds_factory = registry.runtime_port_factory("deepseek")
    assert ds_factory is not None
    ds_port = ds_factory()
    assert ds_port.version().startswith("deepseek:")

    # 3. Assert compute ports
    docker_factory = registry.port_factory("compute", "docker")
    assert docker_factory is not None
    docker_sandbox = docker_factory()
    assert hasattr(docker_sandbox, "run") and hasattr(docker_sandbox, "certify")

    # 4. Assert tools
    mcp_probe_spec = registry.tool_spec("mcp_probe")
    assert mcp_probe_spec is not None
    assert mcp_probe_spec.permission == "readonly"

    docker_tool_spec = registry.tool_spec("docker_sandbox_run")
    assert docker_tool_spec is not None
    assert docker_tool_spec.permission == "side_effect"

    mcp_handler = registry.tool_handler("mcp_probe")
    assert mcp_handler is not None
    res = mcp_handler({"server": "test_env"})
    assert res["ok"] is True
    assert res["server"] == "test_env"

    # 5. Assert hook presence
    hooks = registry._hooks.get("tool_call", [])
    assert len(hooks) >= 2
