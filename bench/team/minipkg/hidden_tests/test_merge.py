"""Spec §4 (layers and merging)."""

import pytest
from confkit import ConfkitError, MergeError, loads


def test_later_assignment_replaces_value_of_any_type():
    cfg = loads("[s]\na = 1\nb = [1, 2]\nc = keep\n", "[s]\na = ['x']\nb = 5\n")
    assert cfg["s"] == {"a": ["x"], "b": 5, "c": "keep"}
    cfg = loads("[s]\na = 1\n", "[s]\na = [1, 'x']\n")
    assert cfg.get("s.a") == [1, "x"]


def test_append_list_across_layers():
    cfg = loads("[s]\nitems = [1, 2]\n", "[s]\nitems += [3, 'four']\n", "[s]\nitems += []\n")
    assert cfg.get("s.items") == [1, 2, 3, "four"]


def test_append_scalar_is_one_item_list():
    cfg = loads("[s]\nitems = ['a']\n", "[s]\nitems += b\nitems += 3\n")
    assert cfg.get("s.items") == ["a", "b", 3]


def test_append_to_absent_key_or_section():
    cfg = loads("[s]\nx = 1\n", '[s]\nnew += 5\n[t]\nl += [1, 2]\nm += "str"\n')
    assert cfg.get("s.new") == [5]
    assert cfg["t"] == {"l": [1, 2], "m": ["str"]}


def test_append_to_non_list_raises():
    for earlier in ["1", "text", "true", "2.5", "'quoted'"]:
        with pytest.raises(MergeError) as info:
            loads(f"[s]\nx = {earlier}\n", "[s]\nx += [1]\n")
        assert isinstance(info.value, ConfkitError)


def test_append_within_one_text_after_assignment():
    cfg = loads("[s]\nx = [1]\nx += 2\nx += [3, 4]\n[t]\ny += a\n[s]\nx += 5\n")
    assert cfg.get("s.x") == [1, 2, 3, 4, 5]
    assert cfg.get("t.y") == ["a"]


def test_layers_merge_keys_and_sections():
    cfg = loads("[b]\nk1 = 1\n[a]\nk = 1\n", "[c]\nz = 0\n[a]\nk = 2\nk2 = 3\n")
    assert cfg.sections() == ["b", "a", "c"]
    assert cfg.to_dict() == {"b": {"k1": 1}, "a": {"k": 2, "k2": 3}, "c": {"z": 0}}
