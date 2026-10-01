"""Typing of value text (spec §2.1) and text rendering shared by dumps/interpolation."""

from __future__ import annotations

import re
from dataclasses import dataclass

_INT_RE = re.compile(r"-?[0-9]+(?:_[0-9]+)*")
_FLOAT_RE = re.compile(r"-?[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


class ValueSyntaxError(Exception):
    """Raised by value typing; the caller converts it to ParseError with line/source."""


@dataclass(frozen=True)
class Template:
    """A string still subject to interpolation.

    ``bare`` is true for unquoted values, where a value consisting of exactly one
    reference keeps the referenced value's type.
    """

    text: str
    bare: bool


def _parse_double_quoted(text: str) -> tuple[str, int]:
    """Parse a double-quoted string starting at text[0]; return (value, end index after quote)."""
    out = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text) or text[i + 1] not in _ESCAPES:
                raise ValueSyntaxError(f"invalid escape sequence in {text!r}")
            out.append(_ESCAPES[text[i + 1]])
            i += 2
        elif ch == '"':
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    raise ValueSyntaxError(f"unterminated string {text!r}")


def _maybe_template(s: str, bare: bool) -> str | Template:
    return Template(s, bare) if "$" in s else s


def _parse_scalar(text: str, bare: bool) -> object | None:
    """Rules 1-5; return None when none applies."""
    if text.startswith('"'):
        value, end = _parse_double_quoted(text)
        if text[end:].strip():
            raise ValueSyntaxError(f"unexpected text after closing quote: {text[end:].strip()!r}")
        return _maybe_template(value, bare=False)
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if _INT_RE.fullmatch(text):
        return int(text.replace("_", ""))
    if _FLOAT_RE.fullmatch(text):
        return float(text)
    return None


def _split_items(inner: str) -> list[str]:
    items, cur, quote, i = [], [], None, 0
    while i < len(inner):
        ch = inner[i]
        if quote == '"' and ch == "\\" and i + 1 < len(inner):
            cur.append(inner[i : i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            items.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    items.append("".join(cur))
    return [item.strip() for item in items]


def _parse_list(text: str) -> list:
    inner = text[1:-1]
    if not inner.strip():
        return []
    items = _split_items(inner)
    if len(items) > 1 and items[-1] == "":
        items.pop()
    result = []
    for item in items:
        if item == "":
            raise ValueSyntaxError("empty list item")
        if item[0] not in "\"'" and ("[" in item or "]" in item):
            raise ValueSyntaxError("nested lists are not allowed")
        value = _parse_scalar(item, bare=False)
        if value is None:
            value = _maybe_template(item, bare=False)
        result.append(value)
    return result


def parse_value(text: str) -> object:
    """Type a (comment-free) value text according to spec §2.1."""
    text = text.strip()
    value = _parse_scalar(text, bare=True)
    if value is not None:
        return value
    if text.startswith("[") and text.endswith("]"):
        return _parse_list(text)
    return _maybe_template(text, bare=True)


def quote_string(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return '"' + escaped.replace("$", "$$") + '"'


def format_value(value: object) -> str:
    """Render a value in the `dumps` text form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    return quote_string(str(value))


def to_text(value: object) -> str:
    """Text used when a referenced value is concatenated into a string."""
    if isinstance(value, str):
        return value
    return format_value(value)
