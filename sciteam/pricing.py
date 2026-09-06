"""Token → USD when the operator configured rates; otherwise 0 with honesty."""

from __future__ import annotations

import os


def _rate(name: str) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def cost_usd(*, prompt_tokens: int = 0, completion_tokens: int = 0) -> float:
    """``SCITEAM_LLM_{INPUT,OUTPUT}_USD_PER_MTOK``; unset rates yield 0.0."""
    inp = _rate("SCITEAM_LLM_INPUT_USD_PER_MTOK")
    out = _rate("SCITEAM_LLM_OUTPUT_USD_PER_MTOK")
    if inp == 0.0 and out == 0.0:
        return 0.0
    return (max(0, int(prompt_tokens)) * inp + max(0, int(completion_tokens)) * out) / 1_000_000.0
