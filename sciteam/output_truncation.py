"""Shared head/tail truncation for tool outputs (own arm, aligned with pi).

Two independent limits — whichever is hit first wins:
- line limit (default 2000)
- byte limit (default 50 KiB)

Truncation always reports flags and original size so the model can continue
(read: next offset) or fetch the full audit copy (bash: full_output_path).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    max_lines: int
    max_bytes: int

    def as_dict(self) -> dict:
        return asdict(self)


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


def _split_lines(content: str) -> list[str]:
    if content == "":
        return []
    return content.splitlines()


def truncate_head(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the start (file reads). Never returns a partial last line."""
    lines = _split_lines(content)
    total_bytes = len(content.encode("utf-8"))
    kept: list[str] = []
    used = 0
    hit: str | None = None
    for line in lines:
        if len(kept) >= max_lines:
            hit = "lines"
            break
        encoded = line.encode("utf-8")
        extra = len(encoded) + (1 if kept else 0)
        if used + extra > max_bytes:
            hit = "bytes"
            break
        kept.append(line)
        used += extra
    out = "\n".join(kept)
    return TruncationResult(
        content=out,
        truncated=hit is not None,
        truncated_by=hit,
        total_lines=len(lines),
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=len(out.encode("utf-8")),
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the end (bash / build logs — errors live at the tail)."""
    lines = _split_lines(content)
    total_bytes = len(content.encode("utf-8"))
    kept_rev: list[str] = []
    used = 0
    hit: str | None = None
    for line in reversed(lines):
        if len(kept_rev) >= max_lines:
            hit = "lines"
            break
        encoded = line.encode("utf-8")
        extra = len(encoded) + (1 if kept_rev else 0)
        if used + extra > max_bytes:
            hit = "bytes"
            break
        kept_rev.append(line)
        used += extra
    kept = list(reversed(kept_rev))
    out = "\n".join(kept)
    return TruncationResult(
        content=out,
        truncated=hit is not None,
        truncated_by=hit,
        total_lines=len(lines),
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=len(out.encode("utf-8")),
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def persist_full_text(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path
