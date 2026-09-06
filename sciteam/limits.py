"""Lab-wide harness limits (R6: no scattered hardcoded defaults).

These numbers are the declared Gen-2 covariates. Campaign runners persist
them next to the E0 file-hash manifest as ``engine_limits.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Unified declared context window (V4 official / dsh DEFAULT_CONTEXT_WINDOW).
CONTEXT_WINDOW = 1_000_000
# Unified single-turn output cap. 8192 is too small for agentic work;
# 384k is the model ceiling and too expensive as a default.
MAX_TOKENS = 32_768

# Model-visible tool-result cap (archive is always the full JSON).
TOOL_RESULT_VISIBLE_CHARS = 200_000
TOOL_RESULT_PREVIEW_CHARS = 400

# Shared with output_truncation / fs_tools (pi-aligned dual limit).
OUTPUT_MAX_LINES = 2000
OUTPUT_MAX_BYTES = 50 * 1024


def _int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def context_window() -> int:
    return _int_env("SCITEAM_LLM_CONTEXT_WINDOW", CONTEXT_WINDOW, minimum=1024)


def max_tokens() -> int:
    return _int_env("SCITEAM_LLM_MAX_TOKENS", MAX_TOKENS, minimum=256)


def tool_result_visible_chars() -> int:
    return _int_env("SCITEAM_TOOL_RESULT_VISIBLE_CHARS", TOOL_RESULT_VISIBLE_CHARS, minimum=1024)


def tool_result_preview_chars() -> int:
    return _int_env("SCITEAM_TOOL_RESULT_PREVIEW_CHARS", TOOL_RESULT_PREVIEW_CHARS, minimum=64)


def output_max_lines() -> int:
    return _int_env("SCITEAM_OUTPUT_MAX_LINES", OUTPUT_MAX_LINES, minimum=1)


def output_max_bytes() -> int:
    return _int_env("SCITEAM_OUTPUT_MAX_BYTES", OUTPUT_MAX_BYTES, minimum=1024)


def snapshot() -> dict[str, Any]:
    """Values actually in force (env overrides applied)."""
    return {
        "context_window": context_window(),
        "max_tokens": max_tokens(),
        "tool_result_visible_chars": tool_result_visible_chars(),
        "tool_result_preview_chars": tool_result_preview_chars(),
        "output_max_lines": output_max_lines(),
        "output_max_bytes": output_max_bytes(),
        "defaults": {
            "context_window": CONTEXT_WINDOW,
            "max_tokens": MAX_TOKENS,
            "tool_result_visible_chars": TOOL_RESULT_VISIBLE_CHARS,
            "tool_result_preview_chars": TOOL_RESULT_PREVIEW_CHARS,
            "output_max_lines": OUTPUT_MAX_LINES,
            "output_max_bytes": OUTPUT_MAX_BYTES,
        },
    }


def write_engine_limits(target: Path) -> dict[str, Any]:
    data = snapshot()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data
