import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def format_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return format_ts(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
