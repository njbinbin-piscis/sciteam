"""Spec §6 (schema validation)."""

import pytest
from confkit import ConfkitError, ValidationError, loads


def _errors(text, schema, **kwargs):
    with pytest.raises(ValidationError) as info:
        loads(text, schema=schema, **kwargs)
    errors = info.value.errors
    assert isinstance(errors, list) and all(isinstance(e, str) for e in errors)
    return errors


def _keys(errors):
    return {e.split(": ", 1)[0] for e in errors}


def test_valid_config_passes_and_int_converts_to_float():
    schema = {
        "s": {
            "name": {"type": "str"},
            "n": {"type": "int"},
            "r": {"type": "float"},
            "on": {"type": "bool"},
            "l": {"type": "list"},
            "f2": {"type": "float"},
        }
    }
    cfg = loads("[s]\nname = x\nn = 3\nr = 2\non = false\nl = []\nf2 = 1.5\n", schema=schema)
    s = cfg["s"]
    assert s == {"name": "x", "n": 3, "r": 2.0, "on": False, "l": [], "f2": 1.5}
    assert type(s["r"]) is float and type(s["n"]) is int


def test_type_mismatches_including_bool_as_int():
    schema = {
        "s": {
            "a": {"type": "int"},
            "b": {"type": "str"},
            "c": {"type": "bool"},
            "d": {"type": "list"},
            "e": {"type": "float"},
            "ok": {"type": "int"},
        }
    }
    errors = _errors("[s]\na = true\nb = 5\nc = yes\nd = 1\ne = text\nok = 1\n", schema)
    assert _keys(errors) == {"s.a", "s.b", "s.c", "s.d", "s.e"}
    assert all(e.startswith(("s.a: ", "s.b: ", "s.c: ", "s.d: ", "s.e: ")) for e in errors)


def test_required_keys():
    schema = {"s": {"need": {"required": True}, "opt": {"type": "int"}}, "gone": {"k": {"required": True}}}
    errors = _errors("[s]\nother = 1\n", schema)
    assert _keys(errors) == {"s.need", "gone.k"}
    cfg = loads("[s]\nneed = 1\n[gone]\nk = x\n", schema=schema)
    assert cfg.get("s.opt") is None


def test_defaults_fill_missing_keys_and_sections():
    schema = {
        "s": {"a": {"type": "int", "default": 10}, "b": {"required": True, "default": "d"}},
        "extra": {"x": {"type": "list", "default": [1, 2]}},
    }
    cfg = loads("[s]\nc = 1\n[t]\nz = 0\n", schema=schema)
    assert cfg["s"] == {"c": 1, "a": 10, "b": "d"}
    assert cfg.get("extra.x") == [1, 2]
    assert cfg.sections() == ["s", "t", "extra"]
    assert loads("[s]\na = 3\n", schema=schema).get("s.a") == 3


def test_numeric_bounds_inclusive():
    schema = {
        "s": {"i": {"type": "int", "min": 1, "max": 10}, "f": {"type": "float", "min": 0.5, "max": 1.5}}
    }
    assert loads("[s]\ni = 1\nf = 1.5\n", schema=schema).get("s.i") == 1
    assert loads("[s]\ni = 10\nf = 0.5\n", schema=schema).get("s.f") == 0.5
    assert _keys(_errors("[s]\ni = 0\nf = 1.6\n", schema)) == {"s.i", "s.f"}
    assert _keys(_errors("[s]\ni = 11\nf = 0.4\n", schema)) == {"s.i", "s.f"}


def test_length_bounds_for_str_and_list():
    schema = {"s": {"name": {"type": "str", "min": 2, "max": 3}, "l": {"type": "list", "min": 1, "max": 2}}}
    assert loads("[s]\nname = ab\nl = [1, 2]\n", schema=schema).get("s.name") == "ab"
    assert loads("[s]\nname = abc\nl = [1]\n", schema=schema).get("s.l") == [1]
    assert _keys(_errors("[s]\nname = a\nl = []\n", schema)) == {"s.name", "s.l"}
    assert _keys(_errors("[s]\nname = abcd\nl = [1, 2, 3]\n", schema)) == {"s.name", "s.l"}


def test_choices():
    schema = {"s": {"mode": {"type": "str", "choices": ["fast", "slow"]}, "n": {"choices": [1, 2]}}}
    assert loads("[s]\nmode = slow\nn = 2\n", schema=schema).get("s.mode") == "slow"
    assert _keys(_errors("[s]\nmode = medium\nn = 3\n", schema)) == {"s.mode", "s.n"}


def test_list_item_types():
    schema = {
        "s": {
            "f": {"type": "list", "items": "float"},
            "i": {"type": "list", "items": "int"},
            "t": {"type": "list", "items": "str"},
        }
    }
    cfg = loads("[s]\nf = [1, 2.5]\ni = [1, 2]\nt = ['a']\n", schema=schema)
    assert cfg.get("s.f") == [1.0, 2.5] and all(type(x) is float for x in cfg.get("s.f"))
    errors = _errors("[s]\nf = [1, 'x']\ni = [1, 2.5]\nt = ['a', 2]\n", schema)
    assert _keys(errors) == {"s.f", "s.i", "s.t"}


def test_strict_sections_and_unchecked_sections():
    schema = {"s": {"__strict__": True, "a": {"type": "int"}}, "loose": {"a": {"type": "int"}}}
    text = "[s]\na = 1\nextra = 2\n[loose]\na = 1\nmore = x\n[other]\nwhatever = [1]\n"
    errors = _errors(text, schema)
    assert _keys(errors) == {"s.extra"}
    assert loads("[s]\na = 1\n[loose]\nmore = 1\n", schema=schema).get("loose.more") == 1


def test_all_errors_collected_and_sorted():
    schema = {
        "zeta": {"k": {"type": "int"}},
        "alpha": {"b": {"required": True}, "a": {"type": "str", "choices": ["x"]}},
        "mid": {"__strict__": True, "q": {"type": "bool"}},
    }
    text = "[zeta]\nk = no\n[alpha]\na = y\n[mid]\nq = 1\nunknown = 1\n"
    with pytest.raises(ConfkitError) as info:
        loads(text, schema=schema)
    assert isinstance(info.value, ValidationError)
    errors = info.value.errors
    assert errors == sorted(errors)
    assert _keys(errors) == {"zeta.k", "alpha.b", "alpha.a", "mid.q", "mid.unknown"}
