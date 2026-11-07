"""Deterministic VCR Recording & Offline Replay Protocol (`sciteam/kernel/vcr.py`).

Records external read/write tool responses in production (`mode="record"`) so that
offline evolution (`sciteam evolve probe` / `promote`) can deterministically replay tool calls
(`mode="replay"`) without hitting live production APIs or databases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sciteam.kernel.canonical import canonical_json, sha256_hex


def compute_vcr_key(tool_name: str, arguments: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 key for `(tool_name, canonical_json(arguments))`."""
    payload = {"tool": tool_name, "arguments": arguments}
    return sha256_hex(canonical_json(payload))


@dataclass
class VcrEntry:
    vcr_key: str
    tool_name: str
    arguments: dict[str, Any]
    content: str
    is_error: bool = False
    side_effect: str = "read"


@dataclass
class VcrCassette:
    """In-memory and disk-backed cassette of recorded tool calls."""

    mode: Literal["off", "record", "replay"] = "off"
    entries: dict[str, VcrEntry] = field(default_factory=dict)
    call_history: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        content: str,
        *,
        is_error: bool = False,
        side_effect: str = "read",
    ) -> str:
        key = compute_vcr_key(tool_name, arguments)
        entry = VcrEntry(
            vcr_key=key,
            tool_name=tool_name,
            arguments=dict(arguments),
            content=content,
            is_error=is_error,
            side_effect=side_effect,
        )
        self.entries[key] = entry
        self.call_history.append(
            {
                "vcr_key": key,
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "content": content,
                "is_error": is_error,
                "side_effect": side_effect,
            }
        )
        return key

    def lookup(self, tool_name: str, arguments: dict[str, Any]) -> VcrEntry | None:
        key = compute_vcr_key(tool_name, arguments)
        return self.entries.get(key)

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "entries": {
                k: {
                    "vcr_key": v.vcr_key,
                    "tool_name": v.tool_name,
                    "arguments": v.arguments,
                    "content": v.content,
                    "is_error": v.is_error,
                    "side_effect": v.side_effect,
                }
                for k, v in sorted(self.entries.items())
            },
            "call_history": self.call_history,
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(
        cls, path: Path, *, mode: Literal["off", "record", "replay"] = "replay"
    ) -> VcrCassette:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries: dict[str, VcrEntry] = {}
        for k, item in (raw.get("entries") or {}).items():
            entries[k] = VcrEntry(
                vcr_key=item["vcr_key"],
                tool_name=item["tool_name"],
                arguments=item["arguments"],
                content=item["content"],
                is_error=bool(item.get("is_error", False)),
                side_effect=str(item.get("side_effect", "read")),
            )
        return cls(
            mode=mode,
            entries=entries,
            call_history=list(raw.get("call_history") or []),
        )
