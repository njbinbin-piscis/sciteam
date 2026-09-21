"""Tests for sciteam-tool-mcp plugin."""

from __future__ import annotations

import pytest
from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginRegistry
from sciteam.wizard_tools import run_wizard_tool
from sciteam_tool_mcp import (
    InMemoryMCPTransport,
    MCPToolBridge,
    register,
)


def test_register_manifest_and_probe_tool() -> None:
    registry = PluginRegistry()
    api = PluginAPI("mcp_test", registry)
    manifest = register(api)

    assert manifest.id == "sciteam-tool-mcp"
    assert manifest.version == "0.1.0"
    assert manifest.sciteam_api_version == PLUGIN_API_VERSION
    assert "tool" in manifest.provides
    assert "hook" in manifest.provides

    spec = registry.tool_spec("mcp_probe")
    assert spec is not None
    assert spec.permission == "readonly"
    assert spec.capability == "mcp.diagnostics.probe"

    handler = registry.tool_handler("mcp_probe")
    assert handler is not None
    res = handler({"server": "postgres_db"})
    assert res["ok"] is True
    assert res["server"] == "postgres_db"


@pytest.mark.asyncio
async def test_in_memory_transport_protocol() -> None:
    transport = InMemoryMCPTransport()
    transport.register_tool(
        name="echo",
        description="Echo arguments",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        handler=lambda args: {"echoed": args.get("msg")},
        read_only=True,
    )

    init_res = await transport.request("initialize")
    assert init_res["protocolVersion"] == "2024-11-05"

    list_res = await transport.request("tools/list")
    assert len(list_res["tools"]) == 1
    assert list_res["tools"][0]["name"] == "echo"
    assert list_res["tools"][0]["annotations"]["readOnly"] is True

    call_res = await transport.request(
        "tools/call",
        {"name": "echo", "arguments": {"msg": "hello"}},
    )
    assert call_res["isError"] is False
    assert "hello" in call_res["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_bridge_discovery_and_execution() -> None:
    transport = InMemoryMCPTransport()
    transport.register_tool(
        name="query_papers",
        description="Search paper database",
        input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
        handler=lambda args: [{"title": "Institution Engineering", "year": 2026}],
        read_only=True,
    )

    bridge = MCPToolBridge(server_name="scholar", transport=transport)
    tools = await bridge.discover_tools()
    assert len(tools) == 1
    spec, handler = tools[0]

    assert spec.name == "mcp_scholar_query_papers"
    assert spec.capability == "mcp.scholar.query_papers"
    assert spec.permission == "readonly"
    assert "Search paper database" in spec.summary

    output = handler({"topic": "institution"})
    assert output["ok"] is True
    assert "Institution Engineering" in output["output"]


def test_bridge_registration_into_plugin_api_and_wizard_tools() -> None:
    transport = InMemoryMCPTransport()
    transport.register_tool(
        name="calc",
        description="Compute sum",
        input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        handler=lambda args: {"sum": args.get("a", 0) + 10},
    )

    registry = PluginRegistry()
    api = PluginAPI("mcp_custom", registry)
    bridge = MCPToolBridge(server_name="math", transport=transport)
    registered_names = bridge.register_into(api)

    assert "mcp_math_calc" in registered_names

    # Check that wizard_tools can dispatch to it via plugins fallback
    res = run_wizard_tool("mcp_math_calc", {"a": 32}, plugins=registry)
    assert res["ok"] is True
    assert "42" in res["output"]


def test_mcp_hook_blocks_traversal_attempts() -> None:
    registry = PluginRegistry()
    api = PluginAPI("mcp_hook_test", registry)
    register(api)

    hooks = registry._hooks.get("tool_call", [])
    assert len(hooks) == 1
    _, hook_fn = hooks[0]

    # Valid call passes
    assert hook_fn({"tool_name": "mcp_scholar_search"}) is None

    # Path traversal attempt is blocked
    blocked = hook_fn({"tool_name": "mcp_scholar_../secrets"})
    assert blocked is not None
    assert blocked.block is True
    assert "Path traversal" in blocked.reason
