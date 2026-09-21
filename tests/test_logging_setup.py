"""docs/28 §7.2 (E7) — structured JSON-lines logging."""

from __future__ import annotations

import json
import logging

from sciteam.logging_setup import JsonLogFormatter, configure_structured_logging


def test_json_formatter_renders_valid_json_with_extra_fields():
    record = logging.LogRecord(
        name="sciteam.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.run_id = "run-123"
    formatted = JsonLogFormatter().format(record)
    payload = json.loads(formatted)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "sciteam.test"
    assert payload["run_id"] == "run-123"


def test_json_formatter_serializes_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="sciteam.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )
    payload = json.loads(JsonLogFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_structured_logging_is_idempotent(capsys):
    logger = configure_structured_logging(logger_name="sciteam.test_idempotent")
    logger = configure_structured_logging(logger_name="sciteam.test_idempotent")
    structured_handlers = [h for h in logger.handlers if getattr(h, "_sciteam_structured", False)]
    assert len(structured_handlers) == 1

    logger.info("one line, please")
    out = capsys.readouterr().err or capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "one line, please"
