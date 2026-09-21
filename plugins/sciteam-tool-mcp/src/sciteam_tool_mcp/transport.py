"""Transports for Model Context Protocol (MCP) communication."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MCPTransport(Protocol):
    """Protocol for sending JSON-RPC 2.0 requests to an MCP server."""

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC request and return the 'result' or raise on error."""
        ...


class InMemoryMCPTransport:
    """In-memory mock transport for unit testing and local in-process MCP tools."""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
        *,
        read_only: bool = False,
    ) -> None:
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "annotations": {"readOnly": read_only},
        }
        self._handlers[name] = handler

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = params or {}
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "in-memory-mcp", "version": "1.0.0"},
            }
        if method == "tools/list":
            return {"tools": list(self._tools.values())}
        if method == "tools/call":
            tool_name = p.get("name", "")
            if tool_name not in self._handlers:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Tool not found: {tool_name}"}],
                }
            arguments = p.get("arguments", {})
            try:
                handler = self._handlers[tool_name]
                res = handler(arguments)
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, dict) and "content" in res:
                    return res
                return {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(res, default=str)}],
                }
            except Exception as exc:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Tool execution failed: {exc}"}],
                }
        raise ValueError(f"Unsupported MCP method: {method}")


class StdioMCPTransport:
    """Manages an external MCP server subprocess communicating via stdio JSON-RPC."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._args = args or []
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )

    async def stop(self) -> None:
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                await self._process.wait()
            except ProcessLookupError:
                pass
            finally:
                self._process = None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            await self.start()
            if self._process is None or self._process.stdin is None or self._process.stdout is None:
                raise RuntimeError("MCP process stdin/stdout not available")

            self._request_id += 1
            rpc_id = self._request_id
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": method,
                "params": params or {},
            }
            line = json.dumps(payload) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()

            resp_line = await self._process.stdout.readline()
            if not resp_line:
                raise RuntimeError("MCP process closed stdout unexpectedly")

            resp_data = json.loads(resp_line.decode("utf-8"))
            if "error" in resp_data:
                err = resp_data["error"]
                raise RuntimeError(f"MCP JSON-RPC error ({err.get('code')}): {err.get('message')}")
            return resp_data.get("result", {})
