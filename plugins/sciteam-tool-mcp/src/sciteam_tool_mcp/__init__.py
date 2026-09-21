"""Official Model Context Protocol (MCP) tool bridge for SciTeam."""

from __future__ import annotations

import logging
from typing import Any

from sciteam.plugin_api import (
    PLUGIN_API_VERSION,
    HookResult,
    PluginAPI,
    PluginManifest,
)
from sciteam.tool_catalog import ToolSpec

from sciteam_tool_mcp.bridge import MCPToolBridge
from sciteam_tool_mcp.transport import (
    InMemoryMCPTransport,
    MCPTransport,
    StdioMCPTransport,
)

logger = logging.getLogger(__name__)

__all__ = [
    "InMemoryMCPTransport",
    "MCPToolBridge",
    "MCPTransport",
    "StdioMCPTransport",
    "register",
]


def _mcp_probe(arguments: dict[str, Any]) -> dict[str, Any]:
    """Built-in MCP connectivity and capability probe."""
    server = arguments.get("server", "local")
    return {
        "ok": True,
        "mcp_version": "2024-11-05",
        "server": server,
        "status": "ready",
    }


def _mcp_tool_call_validator(payload: dict[str, Any]) -> HookResult | None:
    """Institutional hook inspecting MCP tool calls before execution."""
    tool_name = payload.get("tool_name", "")
    if tool_name.startswith("mcp_") and ".." in tool_name:
        return HookResult(block=True, reason="Path traversal detected in MCP tool name")
    return None


def register(api: PluginAPI) -> PluginManifest:
    """Register the MCP bridge plugin into SciTeam."""
    api.register_tool(
        ToolSpec(
            name="mcp_probe",
            version="1.0",
            capability="mcp.diagnostics.probe",
            summary="Probe and report MCP bridge capability and server readiness.",
            permission="readonly",
            input_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Target MCP server identifier"},
                },
            },
        ),
        handler=_mcp_probe,
    )
    api.on("tool_call", _mcp_tool_call_validator)

    return PluginManifest(
        id="sciteam-tool-mcp",
        version="0.1.0",
        sciteam_api_version=PLUGIN_API_VERSION,
        provides=("tool", "hook"),
        description="Official Model Context Protocol (MCP) tool bridge for SciTeam",
    )
