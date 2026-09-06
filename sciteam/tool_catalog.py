"""Typed tool catalog: a retrievable action space.

Instead of dumping every tool into the prompt, a planner retrieves a small
capability-matched subset. Each tool records schema, version, cost, permission,
determinism, data license and a test reference so selection / parameter /
execution errors can be attributed separately.

The catalog is pure data plus retrieval; it never branches on research stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    capability: str  # short capability tag, e.g. "literature.search"
    summary: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    permission: str = "readonly"  # readonly / side_effect / privileged
    deterministic: bool = True
    cost_hint: str = "low"  # low / medium / high
    data_license: str = "unknown"
    keywords: tuple[str, ...] = ()
    test_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "capability": self.capability,
            "summary": self.summary,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "permission": self.permission,
            "deterministic": self.deterministic,
            "cost_hint": self.cost_hint,
            "data_license": self.data_license,
            "keywords": list(self.keywords),
            "test_ref": self.test_ref,
        }

    def _haystack(self) -> str:
        return " ".join(
            [self.name, self.capability, self.summary, " ".join(self.keywords)]
        ).lower()


class ToolCatalog:
    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ToolSpec) -> None:
        if not tool.name.strip():
            raise ValueError("tool name must be non-empty")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        permission: str | None = None,
    ) -> list[ToolSpec]:
        """Rank tools by simple term overlap with the query.

        Deterministic: ties break on tool name. This keeps retrieval auditable
        and reproducible; a fancier retriever can replace it behind the same
        signature.
        """
        terms = [t for t in query.lower().replace("/", " ").replace(".", " ").split() if t]
        scored: list[tuple[int, str, ToolSpec]] = []
        for tool in self._tools.values():
            if permission is not None and tool.permission != permission:
                continue
            hay = tool._haystack()
            score = sum(1 for term in terms if term in hay)
            if score > 0 or not terms:
                scored.append((score, tool.name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored[:limit]]

    def to_manifest(self) -> dict[str, Any]:
        return {name: tool.to_dict() for name, tool in sorted(self._tools.items())}
