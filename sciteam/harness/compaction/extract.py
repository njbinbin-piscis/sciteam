"""Deterministic extraction from a folded region: serialization (pi), file ops (pi),
anchor index and verbatim user messages (Hermes)."""

import json
import re

from sciteam.harness.state import TURN_START_KINDS, Entry


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return f"{text[:n]}\n\n[... {len(text) - n} more characters truncated]"


def serialize_conversation(entries: list[Entry], *, tool_chars: int, thinking_chars: int) -> str:
    """pi serializeConversation; thinking is also truncated (DeepSeek max-effort reasoning is long)."""
    parts: list[str] = []
    for e in entries:
        m = e.msg
        if m.role == "user":
            if m.content:
                label = "[User]" if e.kind in TURN_START_KINDS else "[Harness notice]"
                parts.append(f"{label}: {m.content}")
        elif m.role == "assistant":
            if m.reasoning:
                parts.append(f"[Assistant thinking]: {_truncate(m.reasoning, thinking_chars)}")
            if m.content:
                parts.append(f"[Assistant]: {m.content}")
            if m.tool_calls:
                calls = []
                for tc in m.tool_calls:
                    if tc.arguments is not None:
                        args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}"
                                         for k, v in tc.arguments.items())
                    else:
                        args = tc.arguments_raw
                    calls.append(f"{tc.name}({_truncate(args, tool_chars)})")
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif m.role == "tool" and m.content:
            parts.append(f"[Tool result]: {_truncate(m.content, tool_chars)}")
    return "\n\n".join(parts)


READ_TOOLS = {"read_file"}
WRITE_TOOLS = {"write_file"}
EDIT_TOOLS = {"edit_file"}


def extract_file_ops(entries: list[Entry], read: set[str], modified: set[str]) -> None:
    for e in entries:
        for tc in e.msg.tool_calls:
            path = (tc.arguments or {}).get("path")
            if not isinstance(path, str):
                continue
            if tc.name in READ_TOOLS:
                read.add(path)
            elif tc.name in WRITE_TOOLS | EDIT_TOOLS:
                modified.add(path)


def format_file_ops(read: set[str], modified: set[str]) -> str:
    read_only = sorted(read - modified)
    sections = []
    if read_only:
        sections.append("<read-files>\n" + "\n".join(read_only) + "\n</read-files>")
    if modified:
        sections.append("<modified-files>\n" + "\n".join(sorted(modified)) + "\n</modified-files>")
    return "\n\n" + "\n\n".join(sections) if sections else ""


# Hermes _ANCHOR_PATTERNS; file extensions widened for scientific / C-extension repos.
ANCHOR_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("PRs/issues", re.compile(r"#\d{3,6}\b"), 120),
    ("commits", re.compile(r"\b[0-9a-f]{9,40}\b"), 40),
    ("branches", re.compile(r"\b(?:fix|feat|docs|refactor|chore|salvage|ent)/[A-Za-z0-9._/-]{3,60}"),
     40),
    ("files", re.compile(
        r"\b[\w./-]+/[\w.-]+\.(?:py|pyx|pxd|ts|tsx|js|rs|c|h|cpp|hpp|go|java|rb|md|rst|txt|cfg|ini"
        r"|yaml|yml|json|toml|sh)\b"), 80),
    ("tests", re.compile(r"\b[\w/.-]+\.py::[\w:\[\]-]+"), 60),
    ("errors", re.compile(
        r"\b(?:[A-Z][a-zA-Z]*Error|Exception|ENOSPC|EACCES|SIGKILL|Traceback)\b[^\n]{0,90}"), 40),
    ("handles", re.compile(r"@[A-Za-z0-9-]{3,30}\b"), 40),
    ("urls", re.compile(r"https?://[^\s)\"']{10,110}"), 30),
]


def _region_text(entries: list[Entry]) -> str:
    chunks = []
    for e in entries:
        if e.msg.content:
            chunks.append(e.msg.content)
        for tc in e.msg.tool_calls:
            chunks.append(tc.arguments_raw)
    return "\n".join(chunks)


def build_anchor_index(entries: list[Entry], *, budget_chars: int) -> str:
    text = _region_text(entries)
    if not text:
        return ""
    sections: list[str] = []
    used = 0
    for label, pattern, cap in ANCHOR_PATTERNS:
        counts: dict[str, int] = {}
        last_seen: dict[str, int] = {}
        for n, m in enumerate(pattern.finditer(text)):
            val = m.group(0).strip().rstrip(".,;:")
            counts[val] = counts.get(val, 0) + 1
            last_seen[val] = n
        if not counts:
            continue
        ranked = sorted(counts, key=lambda v: (-counts[v], -last_seen[v]))[:cap]
        line = f"{label}: " + ", ".join(f"{v}(x{counts[v]})" if counts[v] > 1 else v
                                        for v in ranked)
        if used + len(line) > budget_chars:
            break
        sections.append(line)
        used += len(line)
    if not sections:
        return ""
    return ("\n\n## Anchor Index (mechanically extracted, exact)\n" + "\n".join(sections)
            + "\n(Exact identifiers from the compacted region — use these verbatim, and as "
              "recall(query=...) anchors to recover their full context.)")


def build_verbatim_user_section(entries: list[Entry], *, budget_chars: int, max_chars: int) -> str:
    collected: list[str] = []
    used = 0
    for e in reversed(entries):
        if e.kind not in TURN_START_KINDS or not e.msg.content.strip():
            continue
        remaining = budget_chars - used
        if remaining <= 0:
            break
        text = e.msg.content.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + " …[truncated]"
        if len(text) > remaining:
            text = text[:remaining].rstrip() + " …[truncated]"
        collected.append("> " + text.replace("\n", "\n> "))
        used += len(text)
    if not collected:
        return ""
    return ("\n\n## User Messages (verbatim, newest first)\n" + "\n\n".join(collected)
            + "\n(Every real user message from the compacted region, quoted verbatim. These are the "
              "user's actual words and override any paraphrase of them above.)")


def recovery_footer(folded_from: int, folded_to: int) -> str:
    return ("\n\n## Context Recovery\n"
            f"The compacted messages (entries {folded_from}..{folded_to - 1}) remain fully preserved. "
            "If you need any detail this summary does not carry (exact command output, file "
            "contents, error text, earlier reasoning), recover it with recall(query='<keywords>') "
            "or recall(entry=<n>) — do not guess at lost specifics when you can look them up.")


def fallback_summary(entries: list[Entry], previous: str | None, *, max_chars: int = 40_000) -> str:
    """LLM-free summary used while the summarizer is in cooldown."""
    header = ["## Summary unavailable (summarizer failed); mechanical log of compacted work"]
    if previous:
        header += ["", "### Previous summary", previous]
    header += ["", "### Actions (oldest first)"]
    lines: list[str] = []
    pending: dict[str, str] = {}
    for e in entries:
        m = e.msg
        if m.role == "assistant":
            for tc in m.tool_calls:
                a = tc.arguments or {}
                key = a.get("path") or a.get("command") or tc.arguments_raw
                pending[tc.id] = f"{tc.name}({str(key)[:160]})"
            if m.content.strip():
                lines.append(f"- note: {m.content.strip()[:300]}")
        elif m.role == "tool":
            head = m.content.strip().splitlines()[:1]
            tail = m.content.strip().splitlines()[-1:] if m.content.strip() else []
            first = head[0][:160] if head else ""
            last = tail[0][:160] if tail and tail != head else ""
            call = pending.pop(m.tool_call_id or "", "tool")
            lines.append(f"- {call} → {first}" + (f" … {last}" if last else ""))
    budget = max_chars - sum(len(h) + 1 for h in header)
    if sum(len(x) + 1 for x in lines) > budget:
        head_n, tail_budget = [], budget // 4
        used = 0
        for x in lines:
            if used + len(x) + 1 > tail_budget:
                break
            head_n.append(x)
            used += len(x) + 1
        tail_n: list[str] = []
        used = 0
        for x in reversed(lines[len(head_n):]):
            if used + len(x) + 1 > budget - tail_budget:
                break
            tail_n.append(x)
            used += len(x) + 1
        dropped = len(lines) - len(head_n) - len(tail_n)
        lines = head_n + [f"- [... {dropped} actions omitted; use recall ...]"] + tail_n[::-1]
    return "\n".join(header + lines)
