"""Schema validation (spec §6)."""

from __future__ import annotations

import copy

from .errors import ValidationError

_STRICT = "__strict__"


def _check_type(value: object, type_name: str) -> tuple[bool, object]:
    if type_name == "str":
        return isinstance(value, str), value
    if type_name == "bool":
        return isinstance(value, bool), value
    if type_name == "list":
        return isinstance(value, list), value
    is_int = isinstance(value, int) and not isinstance(value, bool)
    if type_name == "int":
        return is_int, value
    if type_name == "float":
        if is_int or isinstance(value, float):
            return True, float(value)
        return False, value
    raise ValueError(f"unknown schema type {type_name!r}")


def _check_value(name: str, value: object, spec: dict, errors: list[str]) -> object:
    if "type" in spec:
        ok, value = _check_type(value, spec["type"])
        if not ok:
            errors.append(f"{name}: expected {spec['type']}, got {type(value).__name__}")
            return value
    if "items" in spec and isinstance(value, list):
        converted = []
        for index, item in enumerate(value):
            ok, item = _check_type(item, spec["items"])
            if not ok:
                errors.append(f"{name}: item {index} expected {spec['items']}, got {type(item).__name__}")
            converted.append(item)
        value = converted
    if isinstance(value, (str, list)):
        measure, what = len(value), "length"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        measure, what = value, "value"
    else:
        measure = None
    if measure is not None:
        if "min" in spec and measure < spec["min"]:
            errors.append(f"{name}: {what} {measure} is below minimum {spec['min']}")
        if "max" in spec and measure > spec["max"]:
            errors.append(f"{name}: {what} {measure} is above maximum {spec['max']}")
    if "choices" in spec and value not in spec["choices"]:
        errors.append(f"{name}: {value!r} is not one of {spec['choices']!r}")
    return value


def validate(data: dict[str, dict], schema: dict) -> dict[str, dict]:
    result = copy.deepcopy(data)
    errors: list[str] = []
    for sec, sec_spec in schema.items():
        for key, spec in sec_spec.items():
            if key == _STRICT:
                continue
            name = f"{sec}.{key}"
            if sec not in result or key not in result[sec]:
                if "default" in spec:
                    result.setdefault(sec, {})[key] = copy.deepcopy(spec["default"])
                elif spec.get("required", False):
                    errors.append(f"{name}: required key is missing")
                continue
            result[sec][key] = _check_value(name, result[sec][key], spec, errors)
        if sec_spec.get(_STRICT) and sec in result:
            for key in result[sec]:
                if key == _STRICT or key not in sec_spec:
                    errors.append(f"{sec}.{key}: unknown key")
    if errors:
        raise ValidationError(sorted(errors))
    return result
