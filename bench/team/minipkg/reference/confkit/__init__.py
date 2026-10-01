"""confkit — a small layered configuration language (reference implementation)."""

from __future__ import annotations

from collections.abc import Mapping
from os import PathLike

from .config import Config
from .errors import ConfkitError, InterpolationError, MergeError, ParseError, ValidationError
from .loader import apply_env, interpolate, merge
from .parser import parse_text
from .schema import validate

__all__ = [
    "Config",
    "ConfkitError",
    "InterpolationError",
    "MergeError",
    "ParseError",
    "ValidationError",
    "load",
    "loads",
]


def _build(sources: list[tuple[str, str]], env, prefix: str, schema) -> Config:
    raw = merge([parse_text(text, source) for text, source in sources])
    if isinstance(env, Mapping):
        apply_env(raw, env, prefix)
    data = interpolate(raw)
    if schema is not None:
        data = validate(data, schema)
    return Config(data)


def loads(*texts: str, env: Mapping[str, str] | None = None, prefix: str = "CONFKIT_", schema=None) -> Config:
    return _build([(text, "<string>") for text in texts], env, prefix, schema)


def load(
    *paths: str | PathLike, env: Mapping[str, str] | None = None, prefix: str = "CONFKIT_", schema=None
) -> Config:
    sources = []
    for path in paths:
        with open(path, encoding="utf-8", newline="") as fh:
            sources.append((fh.read(), str(path)))
    return _build(sources, env, prefix, schema)
