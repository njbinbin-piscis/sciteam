"""Head/tail truncation: dual line/byte limits, recoverable full copy."""

from __future__ import annotations

from sciteam.output_truncation import persist_full_text, truncate_head, truncate_tail


def test_truncate_head_by_lines():
    text = "\n".join(f"L{i}" for i in range(10))
    cut = truncate_head(text, max_lines=3, max_bytes=10_000)
    assert cut.truncated is True
    assert cut.truncated_by == "lines"
    assert cut.content == "L0\nL1\nL2"
    assert cut.total_lines == 10


def test_truncate_head_by_bytes_never_splits_a_line():
    text = "aaaa\nbbbb\ncccc"
    cut = truncate_head(text, max_lines=50, max_bytes=6)
    assert cut.truncated is True
    assert cut.truncated_by == "bytes"
    assert "\n" not in cut.content or cut.content.endswith("aaaa")
    assert "cccc" not in cut.content


def test_truncate_tail_keeps_the_end():
    text = "\n".join(f"L{i}" for i in range(10))
    cut = truncate_tail(text, max_lines=3, max_bytes=10_000)
    assert cut.truncated is True
    assert cut.content == "L7\nL8\nL9"
    assert cut.total_lines == 10


def test_persist_full_text(tmp_path):
    path = persist_full_text(tmp_path / "tool_history", "x.txt", "hello-full")
    assert path.read_text(encoding="utf-8") == "hello-full"
