"""Spec §3 (interpolation)."""

import pytest
from confkit import ConfkitError, InterpolationError, loads


def test_same_section_reference_in_unquoted_string():
    cfg = loads("[web]\nhost = example.org\nurl = http://${host}:8080/x\n")
    assert cfg.get("web.url") == "http://example.org:8080/x"


def test_whole_value_reference_keeps_type():
    text = (
        "[base]\nport = 8080\nratio = 0.5\non = true\ntags = [1, 'a']\n"
        "[s]\nport = ${base.port}\nratio = ${base.ratio}\non = ${base.on}\ntags = ${base.tags}\n"
    )
    s = loads(text)["s"]
    assert s["port"] == 8080 and type(s["port"]) is int
    assert s["ratio"] == 0.5 and type(s["ratio"]) is float
    assert s["on"] is True
    assert s["tags"] == [1, "a"]


def test_reference_splits_at_last_dot():
    text = "[a.b]\nc = deep\n[x]\nv = ${a.b.c}\nw = <${a.b.c}>\n"
    cfg = loads(text)
    assert cfg.get("x.v") == "deep"
    assert cfg.get("x.w") == "<deep>"


def test_concatenation_converts_values_to_text():
    text = "[s]\nf = false\nn = 3\nx = 2.5\nl = [1, 2]\nmsg = f=${f} n=${n} x=${x} l=${l}\n"
    assert loads(text).get("s.msg") == "f=false n=3 x=2.5 l=[1, 2]"


def test_double_quoted_strings_and_list_items_are_interpolated():
    text = '[s]\nhost = h1\nq = "${host} # not a comment"\nl = ["x-${host}", \'y-${host}\', 7]\n'
    s = loads(text)["s"]
    assert s["q"] == "h1 # not a comment"
    assert s["l"] == ["x-h1", "y-${host}", 7]


def test_dollar_escapes_and_literal_dollars():
    text = '[s]\na = cost $$5\nb = "$${a}"\nc = $x and 5$\nd = $ alone\n'
    s = loads(text)["s"]
    assert s == {"a": "cost $5", "b": "${a}", "c": "$x and 5$", "d": "$ alone"}


def test_single_quoted_and_interpolated_values_are_not_rescanned():
    text = "[s]\nlit = '${nothing} $$'\nesc = \"$${lit}\"\ncopy = ${lit}\ncopy2 = ${esc}\nmix = <${lit}>\n"
    s = loads(text)["s"]
    assert s["lit"] == "${nothing} $$"
    assert s["esc"] == "${lit}"
    assert s["copy"] == "${nothing} $$"
    assert s["copy2"] == "${lit}"
    assert s["mix"] == "<${nothing} $$>"


def test_chained_references_resolve_after_merging_layers():
    base = "[s]\na = ${b}\nb = ${t.c}\nlabel = v${a}\n[t]\nc = 1\n"
    override = "[t]\nc = 2\n"
    cfg = loads(base, override)
    assert cfg.get("s.a") == 2
    assert cfg.get("s.b") == 2
    assert cfg.get("s.label") == "v2"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("[s]\na = ${missing_key_q}\n", "missing_key_q"),
        ("[s]\na = x ${nosuch_sec.k}\n", "nosuch_sec"),
    ],
    ids=["missing-key", "missing-section"],
)
def test_missing_reference_raises(text, name):
    with pytest.raises(InterpolationError) as info:
        loads(text)
    assert isinstance(info.value, ConfkitError)
    assert name in str(info.value)


@pytest.mark.parametrize(
    "text",
    ["[s]\na = ${a}\n", "[s]\na = ${t.b}\n[t]\nb = x${s.a}\n"],
    ids=["self", "two-sections"],
)
def test_cycles_raise(text):
    with pytest.raises(InterpolationError):
        loads(text)
