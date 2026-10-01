"""Layer merging (§4), environment overrides (§5) and interpolation (§3)."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from .errors import InterpolationError, MergeError, ParseError
from .parser import Op
from .values import Template, ValueSyntaxError, parse_value, to_text

_MISSING = object()
Raw = dict[str, dict[str, object]]


def merge(layers: list[list[Op]]) -> Raw:
    data: Raw = {}
    for ops in layers:
        for op in ops:
            section = data.setdefault(op.section, {})
            if op.key is None:
                continue
            if op.op == "=":
                section[op.key] = op.value
                continue
            earlier = section.get(op.key, _MISSING)
            if earlier is _MISSING:
                earlier = []
            elif not isinstance(earlier, list):
                raise MergeError(f"cannot append to non-list value {op.section}.{op.key} (line {op.line})")
            addition = op.value if isinstance(op.value, list) else [op.value]
            section[op.key] = [*earlier, *addition]
    return data


def apply_env(data: Raw, env: Mapping[str, str], prefix: str) -> None:
    for name, text in env.items():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        if "__" not in rest:
            continue
        sec, key = (part.lower() for part in rest.split("__", 1))
        if not sec or not key:
            continue
        target = next((s for s in data if s.lower().replace(".", "_") == sec), sec)
        try:
            value = parse_value(text)
        except ValueSyntaxError as exc:
            raise ParseError(str(exc), 1, f"<env {name}>") from None
        data.setdefault(target, {})[key] = value


class _Interpolator:
    def __init__(self, raw: Raw):
        self.raw = raw
        self.done: dict[tuple[str, str], object] = {}
        self.active: set[tuple[str, str]] = set()

    def resolve(self, section: str, key: str) -> object:
        ref = (section, key)
        if ref in self.done:
            return self.done[ref]
        self.active.add(ref)
        value = self.raw[section][key]
        if isinstance(value, list):
            result: object = [self._expand(item, section) for item in value]
        else:
            result = self._expand(value, section)
        self.active.discard(ref)
        self.done[ref] = result
        return result

    def _lookup(self, name: str, section: str) -> object:
        sec, dot, key = name.rpartition(".")
        if not dot:
            sec = section
        if sec not in self.raw or key not in self.raw[sec]:
            raise InterpolationError(f"unresolved reference ${{{name}}}")
        if (sec, key) in self.active:
            raise InterpolationError(f"reference cycle through ${{{name}}}")
        return copy.deepcopy(self.resolve(sec, key))

    def _expand(self, value: object, section: str) -> object:
        if not isinstance(value, Template):
            return value
        text = value.text
        if value.bare and text.startswith("${") and text.find("}") == len(text) - 1:
            return self._lookup(text[2:-1], section)
        out: list[str] = []
        i = 0
        while i < len(text):
            if text.startswith("$$", i):
                out.append("$")
                i += 2
            elif text.startswith("${", i) and "}" in text[i:]:
                end = text.index("}", i)
                out.append(to_text(self._lookup(text[i + 2 : end], section)))
                i = end + 1
            else:
                out.append(text[i])
                i += 1
        return "".join(out)


def interpolate(raw: Raw) -> Raw:
    interp = _Interpolator(raw)
    return {sec: {key: interp.resolve(sec, key) for key in keys} for sec, keys in raw.items()}
