"""Structured (JSON-lines) logging — 28-sciteam-productization-roadmap.md §7.2 (E7).

The rest of this package already logs through the stdlib `logging` module
(`logger = logging.getLogger(__name__)` in `resilience.py`, `wizard_tools.py`,
`composition.py`, ...). What was missing for a headless/unattended product is
a single, opt-in way to make those records machine-parseable (one JSON
object per line: timestamp, level, logger name, message, exception, and any
`extra=` fields) instead of relying on each entry point to hand-roll its own
`logging.basicConfig(format=...)`.

This module intentionally does not change the default behavior of any
existing logger — nothing calls `configure_structured_logging()`
automatically. A CLI/SDK entry point opts in explicitly.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

#: Attributes every stdlib LogRecord carries that are not "extra" fields —
#: used to separate caller-supplied `extra={...}` from the record's own
#: bookkeeping when serializing.
_STANDARD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__.keys())


class JsonLogFormatter(logging.Formatter):
    """Renders one `logging.LogRecord` as one JSON object per line.

    Any `extra={...}` fields passed to the logging call (e.g.
    ``logger.info("...", extra={"run_id": run_id})``) are folded into the
    JSON object alongside the standard fields, so operators can `jq` on
    run/mission/seat identifiers without parsing free-text messages.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_structured_logging(
    *,
    level: int = logging.INFO,
    logger_name: str = "sciteam",
) -> logging.Logger:
    """Attach a JSON-lines stream handler to `logger_name` (default: the
    `sciteam` package logger, which every module under it inherits from).

    Idempotent: calling this more than once replaces the handler it
    previously installed rather than stacking duplicate handlers, so a
    CLI/SDK entry point can call it unconditionally at startup.
    """
    target = logging.getLogger(logger_name)
    target.setLevel(level)
    for existing in list(target.handlers):
        if getattr(existing, "_sciteam_structured", False):
            target.removeHandler(existing)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler._sciteam_structured = True  # type: ignore[attr-defined]
    target.addHandler(handler)
    target.propagate = False
    return target
