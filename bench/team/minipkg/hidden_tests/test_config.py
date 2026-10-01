"""Spec §1 (`load`) and §7 (`Config` API and `dumps`)."""

import pytest
from confkit import Config, ParseError, load, loads


def test_sections_order_across_layers():
    cfg = loads("[b]\nx = 1\n[a]\ny = 2\n", "[c]\nz = 3\n[b]\nw = 4\n", env={"CONFKIT_ENVSEC__K": "v"})
    assert isinstance(cfg, Config)
    assert cfg.sections() == ["b", "a", "c", "envsec"]


def test_getitem_returns_copy_and_contains():
    cfg = loads("[s]\na = 1\n")
    section = cfg["s"]
    assert type(section) is dict and section == {"a": 1}
    section["a"] = 99
    section["new"] = 1
    assert cfg["s"] == {"a": 1}
    assert "s" in cfg and "t" not in cfg
    with pytest.raises(KeyError):
        cfg["t"]


def test_get_splits_at_last_dot_and_defaults():
    cfg = loads("[a.b]\nc = 1\n[a]\nx = 2\n")
    assert cfg.get("a.b.c") == 1
    assert cfg.get("a.x") == 2
    assert cfg.get("a.b.missing") is None
    assert cfg.get("nope.x", default=5) == 5
    assert cfg.get("a.missing", "d") == "d"


def test_to_dict_is_a_deep_plain_copy():
    cfg = loads("[s]\nl = [1, 2]\n[t]\nk = v\n")
    d = cfg.to_dict()
    assert d == {"s": {"l": [1, 2]}, "t": {"k": "v"}}
    assert type(d) is dict and type(d["s"]) is dict and type(d["s"]["l"]) is list
    d["s"]["l"].append(3)
    d["t"]["k"] = "changed"
    assert cfg.to_dict() == {"s": {"l": [1, 2]}, "t": {"k": "v"}}


def test_equality_compares_contents():
    a = loads("[s]\nx = 1\ny = [1, 'a']\n")
    b = loads("[s]\nx = 0\n", '[s]\nx = ${s.z}\ny = [1, "a"]\nz = 1\n', "[s]\nz = 1\n")
    assert a != b
    c = loads("[s]\ny = [1]\ny += a\nx = 1\n")
    assert a == c


def test_dumps_format():
    cfg = loads("[a]\nx = 1\ny = hello\nz = true\n[b.c]\nf = 1.5\nl = [1, 'q', false]\ne = []\n")
    blocks = cfg.dumps().strip().split("\n\n")
    assert [block.splitlines()[0] for block in blocks] == ["[a]", "[b.c]"]
    assert set(blocks[0].splitlines()[1:]) == {"x = 1", 'y = "hello"', "z = true"}
    assert set(blocks[1].splitlines()[1:]) == {"f = 1.5", 'l = [1, "q", false]', "e = []"}


def test_dumps_round_trip():
    text = (
        "[s]\n"
        "q = 'say \"hi\" \\ back'\n"
        'nl = "line1\\nline2\\tend"\n'
        "dollar = 'cost $5 ${not} $$'\n"
        "comment = 'a # b ; c'\n"
        "empty = ''\n"
        "neg = -3\n"
        "fl = -0.25\n"
        "big = 1_000_000\n"
        "b = FALSE\n"
        "l = ['x, y', '[z]', \"$${raw}\", 2.5, true, '']\n"
        "[other.sec]\n"
        "k = plain words\n"
    )
    cfg = loads(text)
    dumped = cfg.dumps()
    assert '"cost $$5 $${not} $$$$"' in dumped
    again = loads(dumped)
    assert again == cfg
    assert again.sections() == ["s", "other.sec"]
    assert again.dumps() == dumped


def test_load_files_str_and_path(tmp_path):
    p1 = tmp_path / "base.conf"
    p1.write_text("[s]\nname = héllo\nitems = [1]\n", encoding="utf-8")
    p2 = tmp_path / "over.conf"
    p2.write_text("[s]\nitems += 2\n", encoding="utf-8")
    cfg = load(str(p1), p2)
    assert cfg["s"] == {"name": "héllo", "items": [1, 2]}
    bad = tmp_path / "bad.conf"
    bad.write_text("[s]\nok = 1\n???\n", encoding="utf-8")
    for arg in (bad, str(bad)):
        with pytest.raises(ParseError) as info:
            load(p1, arg)
        assert info.value.line == 3
        assert info.value.source == str(bad)
