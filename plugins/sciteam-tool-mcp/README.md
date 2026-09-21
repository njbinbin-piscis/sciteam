# sciteam-tool-mcp

Official Model Context Protocol (MCP) tool bridge plugin for [SciTeam](https://github.com/njbinbin-piscis/sciteam).

Connects SciTeam agents occupying institutional seats to external tools and data sources
exposed via MCP (JSON-RPC over stdio, HTTP/SSE, or in-process).

## Institutional Governance of MCP Tools

In SciTeam, external MCP tools never bypass the institutional boundary:
1. **Catalog Integrity**: MCP tools are registered as first-class `ToolSpec` definitions with explicit capabilities and permissions (`readonly` vs `side_effect`).
2. **Audit & Pre-Execution Hook**: Every tool invocation passes through SciTeam's `tool_call` institutional hook, enabling gatekeepers and assessors to audit or veto tool execution before actions take effect.
3. **Structured Outcomes**: Non-fatal tool errors (e.g., parameter validation, server timeouts) return structured `{"ok": False, "error": ...}` results rather than crashing the orchestrator loop.

## Installation

```bash
pip install sciteam-tool-mcp
```

## Usage

```python
from sciteam.plugin_api import load_all_plugins
from sciteam_tool_mcp import MCPToolBridge, InMemoryMCPTransport

# Connect to an MCP server and register its tools dynamically
transport = InMemoryMCPTransport()
transport.register_tool(
    name="query_database",
    description="Query SQL database",
    input_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
    handler=lambda args: {"rows": [{"id": 1, "value": "demo"}]},
)

bridge = MCPToolBridge(server_name="db", transport=transport)
# Register into your SciTeam PluginAPI during startup
```
