"""Line-level syntax (spec §2): comments, continuations, headers and assignments."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ParseError
from .values import ValueSyntaxError, parse_value

_HEADER_RE = re.compile(r"\[([A-Za-z0-9_.-]+)\]")
_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*(\+=|=)\s*(.*)", re.DOTALL)


@dataclass
class Op:
    section: str
    key: str | None  # None: bare section header
    op: str = ""
    value: object = None
    line: int = 0


def strip_comment(line: str) -> str:
    """Remove an inline comment: '#' or ';' outside quotes and preceded by whitespace."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "#;" and i > 0 and line[i - 1].isspace():
            return line[:i]
        i += 1
    return line


def _logical_lines(text: str):
    """Yield (first_line_number, logical_line) with comments removed and continuations joined."""
    physical = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    parts: list[str] = []
    start = 0
    for number, raw in enumerate(physical, start=1):
        if not parts:
            stripped = raw.strip()
            if not stripped or stripped[0] in "#;":
                continue
            start = number
        content = strip_comment(raw).rstrip()
        if content.endswith("\\"):
            parts.append(content[:-1].strip())
            continue
        parts.append(content.strip())
        yield start, " ".join(parts)
        parts = []
    if parts:
        yield start, " ".join(parts)


def parse_text(text: str, source: str) -> list[Op]:
    ops: list[Op] = []
    section: str | None = None
    assigned: set[tuple[str, str]] = set()
    for line_no, line in _logical_lines(text):
        if line.startswith("["):
            m = _HEADER_RE.fullmatch(line)
            if not m:
                raise ParseError(f"malformed section header {line!r}", line_no, source)
            section = m.group(1)
            ops.append(Op(section, None, line=line_no))
            continue
        m = _ASSIGN_RE.fullmatch(line)
        if not m:
            raise ParseError(f"invalid line {line!r}", line_no, source)
        key, op, value_text = m.groups()
        if section is None:
            raise ParseError(f"key {key!r} assigned before any section header", line_no, source)
        if op == "=":
            if (section, key) in assigned:
                raise ParseError(f"duplicate key {section}.{key}", line_no, source)
            assigned.add((section, key))
        try:
            value = parse_value(value_text)
        except ValueSyntaxError as exc:
            raise ParseError(str(exc), line_no, source) from None
        ops.append(Op(section, key, op, value, line_no))
    return ops
