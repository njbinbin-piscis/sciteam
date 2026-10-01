"""The `Config` result object (spec §7)."""

from __future__ import annotations

import copy

from .values import format_value


class Config:
    def __init__(self, data: dict[str, dict]):
        self._data = copy.deepcopy(data)

    def sections(self) -> list[str]:
        return list(self._data)

    def __getitem__(self, section: str) -> dict:
        return copy.deepcopy(self._data[section])

    def __contains__(self, section: object) -> bool:
        return section in self._data

    def get(self, name: str, default=None):
        section, _, key = name.rpartition(".")
        try:
            return copy.deepcopy(self._data[section][key])
        except KeyError:
            return default

    def to_dict(self) -> dict[str, dict]:
        return copy.deepcopy(self._data)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Config):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Config({self._data!r})"

    def dumps(self) -> str:
        blocks = []
        for section, keys in self._data.items():
            lines = [f"[{section}]"] + [f"{key} = {format_value(value)}" for key, value in keys.items()]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) + "\n"
