"""Catalog-backed capability registry for worker tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ToolHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class CatalogTool:
    name: str
    capability: str
    permission: str
    summary: str
    deterministic: bool = False


class ToolRegistry:
    def __init__(self, catalog_path: Path | str) -> None:
        raw = yaml.safe_load(Path(catalog_path).read_text(encoding="utf-8")) or {}
        self._tools = {
            str(row["name"]): CatalogTool(
                name=str(row["name"]),
                capability=str(row["capability"]),
                permission=str(row.get("permission") or "readonly"),
                summary=str(row.get("summary") or ""),
                deterministic=bool(row.get("deterministic", False)),
            )
            for row in (raw.get("tools") or [])
            if isinstance(row, dict) and row.get("name") and row.get("capability")
        }
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, capability: str, handler: ToolHandler) -> None:
        self._handlers[str(capability)] = handler

    @property
    def available_capabilities(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def permission_of(self, name: str) -> str:
        """Catalog permission for a tool name; unknown tools are treated as
        `side_effect` (the conservative/serial default) rather than silently
        granted `readonly` concurrency."""
        tool = self._tools.get(str(name))
        return tool.permission if tool is not None else "side_effect"

    def deterministic_of(self, name: str) -> bool:
        """Whether the catalog marks this tool `deterministic: true` (P1-7):
        same-round (tool, args) result reuse is only safe for tools whose
        catalog entry declares this — e.g. `literature_search`/`run_eval`,
        not `web_search`/`submit_job`. Unknown tools default to False."""
        tool = self._tools.get(str(name))
        return bool(tool.deterministic) if tool is not None else False

    def definitions_for(
        self,
        capabilities: set[str] | frozenset[str],
        definitions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed_names = {
            tool.name
            for tool in self._tools.values()
            if tool.capability in capabilities and tool.capability in self._handlers
        }
        return [
            definition
            for definition in definitions
            if str((definition.get("function") or {}).get("name") or "") in allowed_names
        ]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool = self._tools.get(str(name))
        if tool is None:
            return {
                "ok": False,
                "reason_code": "unknown_tool",
                "error": f"tool not present in catalog: {name}",
            }
        handler = self._handlers.get(tool.capability)
        if handler is None:
            return {
                "ok": False,
                "reason_code": "capability_unavailable",
                "error": f"capability not injected: {tool.capability}",
            }
        if tool.permission == "privileged" and not bool((context or {}).get("allow_privileged")):
            return {
                "ok": False,
                "reason_code": "privileged_tool_denied",
                "error": f"privileged tool requires explicit grant: {name}",
            }
        try:
            return handler(
                arguments if isinstance(arguments, dict) else {},
                dict(context or {}),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "reason_code": "tool_execution_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "tool": name,
            }
