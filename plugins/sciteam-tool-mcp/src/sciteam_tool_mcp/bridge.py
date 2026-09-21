"""Bridge mapping Model Context Protocol (MCP) tools to SciTeam ToolSpecs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

from sciteam.plugin_api import PluginAPI
from sciteam.tool_catalog import ToolSpec

from sciteam_tool_mcp.transport import MCPTransport

logger = logging.getLogger(__name__)


def _run_sync(coro: Any) -> Any:
    """Run an async coroutine synchronously, safe whether in an existing loop or not."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class MCPToolBridge:
    """Bridges tools from an MCP server into SciTeam ToolSpecs and Handlers."""

    def __init__(
        self,
        server_name: str,
        transport: MCPTransport,
        *,
        prefix: str | None = None,
    ) -> None:
        self._server_name = server_name
        self._transport = transport
        self._prefix = prefix or f"mcp_{server_name}_"

    async def initialize(self) -> dict[str, Any]:
        """Perform MCP protocol handshake."""
        return await self._transport.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sciteam-mcp-bridge", "version": "0.1.0"},
            },
        )

    async def discover_tools(self) -> list[tuple[ToolSpec, Any]]:
        """Query MCP tools/list and construct SciTeam (ToolSpec, handler) pairs."""
        res = await self._transport.request("tools/list")
        raw_tools = res.get("tools", [])
        registrations: list[tuple[ToolSpec, Any]] = []

        for t in raw_tools:
            name = t.get("name", "")
            description = t.get("description", "")
            input_schema = t.get("inputSchema", {})
            annotations = t.get("annotations", {})
            is_readonly = annotations.get("readOnly", False)

            spec_name = f"{self._prefix}{name}"
            spec = ToolSpec(
                name=spec_name,
                version="1.0",
                capability=f"mcp.{self._server_name}.{name}",
                summary=description or f"MCP tool {name} from {self._server_name}",
                permission="readonly" if is_readonly else "side_effect",
                input_schema=input_schema if input_schema else {},
            )

            handler = self._create_handler(name)
            registrations.append((spec, handler))

        return registrations

    def _create_handler(self, tool_name: str) -> Any:
        async def _call_mcp(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                res = await self._transport.request(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                )
                is_error = res.get("isError", False)
                content = res.get("content", [])
                text_outputs = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_outputs.append(item.get("text", ""))

                output_str = "\n".join(text_outputs)
                return {
                    "ok": not is_error,
                    "output": output_str,
                    "content": content,
                }
            except Exception as exc:
                logger.warning("MCP tool call %s failed: %s", tool_name, exc)
                return {
                    "ok": False,
                    "error": str(exc),
                }

        def _sync_handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return _run_sync(_call_mcp(arguments))

        return _sync_handler

    def register_into(self, api: PluginAPI) -> list[str]:
        """Synchronously discover tools and register them into a PluginAPI."""
        tools = _run_sync(self.discover_tools())
        registered_names: list[str] = []
        for spec, handler in tools:
            api.register_tool(spec, handler)
            registered_names.append(spec.name)
        return registered_names
