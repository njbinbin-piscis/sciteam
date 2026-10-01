from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from sciteam.harness.config import HarnessConfig
from sciteam.harness.state import ActivationState
from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.clock import Clock
from sciteam.providers.llm.types import ToolSpec
from sciteam.providers.sandbox.base import Sandbox


class ToolResult(BaseModel):
    content: str
    is_error: bool = False
    meta: dict[str, Any] = {}


@dataclass
class ToolContext:
    sandbox: Sandbox
    blobs: BlobStore
    state: ActivationState
    config: HarnessConfig
    clock: Clock
    remaining_wall_s: float = float("inf")
    extras: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    spec: ToolSpec
    readonly: bool

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class ToolArgError(Exception):
    pass


def validate_args(spec: ToolSpec, args: dict[str, Any]) -> None:
    """Minimal JSON-schema check: required keys and primitive types."""
    schema = spec.parameters
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            raise ToolArgError(f"missing required argument '{key}'")
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                "array": list, "object": dict}
    for key, val in args.items():
        if key not in props:
            if schema.get("additionalProperties", True) is False:
                raise ToolArgError(f"unknown argument '{key}'")
            continue
        t = props[key].get("type")
        if t in type_map and val is not None:
            if t == "integer" and isinstance(val, bool):
                raise ToolArgError(f"argument '{key}' must be integer")
            if not isinstance(val, type_map[t]):
                raise ToolArgError(f"argument '{key}' must be {t}, got {type(val).__name__}")
        enum = props[key].get("enum")
        if enum and val not in enum:
            raise ToolArgError(f"argument '{key}' must be one of {enum}")


def cap_output(text: str, *, max_chars: int, head_fraction: float, blobs: BlobStore
               ) -> tuple[str, str | None]:
    """Head+tail truncation; the full text goes to the BlobStore for read_output."""
    if len(text) <= max_chars:
        return text, None
    ref = blobs.put_text(text).sha256
    head = int(max_chars * head_fraction)
    tail = max_chars - head
    n_lines = text.count("\n") + 1
    omitted = len(text) - head - tail
    note = (f"\n\n[... {omitted} characters omitted (total {len(text)} chars, {n_lines} lines). "
            f"Full output: read_output(ref='{ref[:16]}', offset=<line>, limit=<lines>) ...]\n\n")
    return text[:head] + note + text[-tail:], ref
