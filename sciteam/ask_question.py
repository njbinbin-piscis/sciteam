"""ask_question — structured operator questions for SciTeam wizard.

Schema: `paper/lab/contracts/ask_question.schema.json` (single question).
Tool wrapper accepts 1–3 questions and is a terminal wizard action.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "ask_question.schema.json"

_KINDS = frozenset({"quick_pick", "multi_select", "text_input"})


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def normalize_question(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one question object; raise ValueError if invalid."""
    if not isinstance(raw, dict):
        raise ValueError("question must be an object")
    qid = str(raw.get("id") or "").strip()
    prompt = str(raw.get("prompt") or raw.get("question") or "").strip()
    if not qid or not prompt:
        raise ValueError("question requires non-empty id and prompt")

    kind = str(raw.get("kind") or "").strip()
    allow_multiple = bool(raw.get("allow_multiple"))
    if not kind:
        kind = "multi_select" if allow_multiple else "quick_pick"
        if raw.get("multiline") or (not raw.get("options") and raw.get("allow_free_text", True)):
            if not raw.get("options"):
                kind = "text_input"
    if kind not in _KINDS:
        raise ValueError(f"unsupported ask_question kind: {kind}")

    options_in = raw.get("options") or []
    options: list[dict[str, str]] = []
    if isinstance(options_in, list):
        for i, opt in enumerate(options_in):
            if isinstance(opt, str):
                options.append({"id": f"opt_{i}", "label": opt})
            elif isinstance(opt, dict):
                oid = str(opt.get("id") or f"opt_{i}")
                label = str(opt.get("label") or oid)
                options.append({"id": oid, "label": label})

    if kind in {"quick_pick", "multi_select"} and len(options) < 2:
        # Downgrade rather than fail — models often send one option early.
        kind = "text_input"
        allow_multiple = False

    return {
        "id": qid,
        "prompt": prompt,
        "kind": kind,
        "allow_multiple": allow_multiple or kind == "multi_select",
        "allow_free_text": bool(raw.get("allow_free_text", True)),
        "required": bool(raw.get("required", True)),
        "placeholder": str(raw.get("placeholder") or ""),
        "multiline": bool(raw.get("multiline", kind == "text_input")),
        "min_length": int(raw.get("min_length") or 0),
        "options": options,
    }


def soft_normalize_questions(items: Any) -> list[dict[str, Any]]:
    """Best-effort normalize: coerce single object; skip/repair bad items; never raise."""
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            out.append(normalize_question(item))
            continue
        except ValueError:
            pass
        prompt = str(item.get("prompt") or item.get("question") or item.get("text") or "").strip()
        if not prompt:
            continue
        qid = str(item.get("id") or f"q_{i}").strip() or f"q_{i}"
        out.append(
            {
                "id": qid,
                "prompt": prompt,
                "kind": "text_input",
                "allow_multiple": False,
                "allow_free_text": True,
                "required": True,
                "placeholder": str(item.get("placeholder") or ""),
                "multiline": True,
                "min_length": 0,
                "options": [],
            }
        )
    return out


def normalize_questions(items: list[Any] | None) -> list[dict[str, Any]]:
    """Strict normalize (raises). Prefer soft_normalize_questions for UI paths."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(normalize_question(item))
    return out


def _question_item_schema() -> dict[str, Any]:
    schema = load_schema()
    # Drop meta keys for nested item use
    return {
        "type": "object",
        "properties": schema.get("properties") or {},
        "required": ["id", "prompt"],
        "additionalProperties": True,
    }


def tool_schema() -> dict[str, Any]:
    """OpenAI function tool definition for ask_question (terminal turn)."""
    return {
        "type": "function",
        "function": {
            "name": "ask_question",
            "description": (
                "Present 1–3 structured questions to the operator (quick_pick / "
                "multi_select / text_input). This ENDS the current tool loop — "
                "do not also emit a final JSON with empty questions. "
                "Use this when you have topic options or config gaps to resolve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assistant_message": {
                        "type": "string",
                        "description": "Short Chinese preface shown above the questions",
                    },
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": _question_item_schema(),
                    },
                    "intake_patch": {
                        "type": "object",
                        "description": "Optional fields to merge into intake state",
                    },
                    "blockers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["questions"],
            },
        },
    }


def execute_ask_question(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Validate tool args; return a terminal ask_question turn payload."""
    args = arguments if isinstance(arguments, dict) else {}
    qs = args.get("questions")
    if qs is None and args.get("id") and (args.get("prompt") or args.get("question")):
        qs = [args]
    questions = soft_normalize_questions(qs)
    if not questions:
        return {
            "ok": False,
            "error": "ask_question requires questions[] with id+prompt",
            "terminal": False,
        }
    blockers = args.get("blockers") or []
    if not isinstance(blockers, list):
        blockers = [str(blockers)]
    patch = args.get("intake_patch")
    if not isinstance(patch, dict):
        patch = {}
    return {
        "ok": True,
        "terminal": True,
        "type": "ask_question",
        "assistant_message": str(args.get("assistant_message") or args.get("message") or ""),
        "questions": questions,
        "intake_patch": patch,
        "blockers": [str(b) for b in blockers],
        "research_plan": None,
        "summary": None,
    }
