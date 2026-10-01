"""Spec §2 (syntax) and §2.1 (values)."""

import pytest
from confkit import ConfkitError, ParseError, loads


def test_sections_and_assignments():
    cfg = loads("[main]\nname=alpha\n   other   =   beta gamma   \n  [server.http]  \nmy-key_2 = x\n")
    assert cfg.sections() == ["main", "server.http"]
    assert cfg["main"] == {"name": "alpha", "other": "beta gamma"}
    assert cfg["server.http"] == {"my-key_2": "x"}
    assert cfg.get("main.name") == "alpha"


def test_blank_lines_comment_lines_and_crlf():
    text = (
        "# top comment\r\n; another\r\n\r\n[s]\r\n"
        "   # indented comment\r\n\t; indented\r\na = 1\r\nb = two\r\n"
    )
    cfg = loads(text)
    assert cfg.to_dict() == {"s": {"a": 1, "b": "two"}}


def test_repeated_section_header_accumulates():
    cfg = loads("[a]\nx = 1\n[b]\ny = 2\n[a]\nz = 3\n")
    assert cfg["a"] == {"x": 1, "z": 3}
    assert cfg["b"] == {"y": 2}


def test_inline_comments():
    text = "[s] # header comment\na = value # comment\nb = value ; comment\nc = x#y\nd = x;y\ne = 5\t# tab\n"
    cfg = loads(text)
    assert cfg["s"] == {"a": "value", "b": "value", "c": "x#y", "d": "x;y", "e": 5}


def test_comment_characters_inside_quotes_are_kept():
    text = '[s]\na = "x # y ; z" # real\nb = \'p # q\' ; real\nc = "say \\"hi\\" # still" # gone\n'
    cfg = loads(text)
    assert cfg["s"] == {"a": "x # y ; z", "b": "p # q", "c": 'say "hi" # still'}


def test_continuation_lines():
    text = "[s]\na = one   \\\n     two\nb = x \\\n  y \\\n  z\nc = first \\ # note\n  second\nd = 1\n"
    cfg = loads(text)
    assert cfg["s"] == {"a": "one two", "b": "x y z", "c": "first second", "d": 1}


def test_continued_assignment_error_reports_first_line():
    text = "[s]\nok = 1\nbad key \\\n = 5\n"
    with pytest.raises(ParseError) as info:
        loads(text)
    assert info.value.line == 3


def test_quoted_strings():
    text = (
        "[s]\n"
        'a = "tab\\there\\nnew \\"q\\" back\\\\slash"\n'
        "b = 'raw \\n \\q \"dq\"'\n"
        'c = "true"\n'
        "d = '42'\n"
        'e = ""\n'
    )
    cfg = loads(text)
    assert cfg["s"] == {
        "a": 'tab\there\nnew "q" back\\slash',
        "b": 'raw \\n \\q "dq"',
        "c": "true",
        "d": "42",
        "e": "",
    }


def test_bools_ints_floats():
    text = (
        "[s]\nb1 = true\nb2 = FALSE\nb3 = True\n"
        "i1 = 42\ni2 = -7\ni3 = 1_000\ni4 = -1_2_3\n"
        "f1 = 3.25\nf2 = -1.5\nf3 = 2.0e-3\nf4 = 1.5E+3\n"
    )
    s = loads(text)["s"]
    assert s["b1"] is True and s["b2"] is False and s["b3"] is True
    assert [s[k] for k in ("i1", "i2", "i3", "i4")] == [42, -7, 1000, -123]
    assert all(type(s[k]) is int for k in ("i1", "i2", "i3", "i4"))
    assert [s[k] for k in ("f1", "f2", "f3", "f4")] == [3.25, -1.5, 0.002, 1500.0]
    assert all(type(s[k]) is float for k in ("f1", "f2", "f3", "f4"))


def test_non_numbers_are_unquoted_strings():
    text = "[s]\na = 1.\nb = .5\nc = 1__000\nd = 1_\ne = +5\nf = 1e5\ng = yes\nh =\ni = 12abc\nj = a=b\n"
    s = loads(text)["s"]
    assert s == {
        "a": "1.",
        "b": ".5",
        "c": "1__000",
        "d": "1_",
        "e": "+5",
        "f": "1e5",
        "g": "yes",
        "h": "",
        "i": "12abc",
        "j": "a=b",
    }


def test_lists():
    text = (
        "[s]\n"
        "empty = []\n"
        "nums = [1, -2, 3.5, true]\n"
        "trailing = [1, 2,]\n"
        "quoted = [\"a, b\", '[x]', \"c]\" ,  'd,e'  ]\n"
        "spaced = [  1 ,2  ]  # comment\n"
    )
    s = loads(text)["s"]
    assert s["empty"] == []
    assert s["nums"] == [1, -2, 3.5, True]
    assert type(s["nums"][2]) is float and s["nums"][3] is True
    assert s["trailing"] == [1, 2]
    assert s["quoted"] == ["a, b", "[x]", "c]", "d,e"]
    assert s["spaced"] == [1, 2]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("# c\n\nkey = 1\n[s]\n", 3),
        ("[s]\na = 1\n[bad name]\n", 3),
        ("[s]\n[unclosed\n", 2),
        ("[s]\na = 1\njust some text\n", 3),
        ("[s]\n1abc = 2\n", 2),
        ("[s]\na.b = 1\n", 2),
        ("[s]\na = 1\n\n# c\na = 2\n", 5),
        ("[s]\na = 1\n[t]\n[s]\na = 3\n", 5),
        ('[s]\nok = 1\nx = "bad \\q escape"\n', 3),
        ('[s]\nx = "done" trailing\n', 2),
        ("[s]\nx = [1, [2, 3]]\n", 2),
    ],
    ids=[
        "key-before-section",
        "bad-header-name",
        "unclosed-header",
        "garbage-line",
        "bad-key-start",
        "dotted-key",
        "duplicate-key",
        "duplicate-key-repeated-section",
        "bad-escape",
        "text-after-quote",
        "nested-list",
    ],
)
def test_parse_errors(text, line):
    with pytest.raises(ParseError) as info:
        loads(text)
    err = info.value
    assert isinstance(err, ConfkitError)
    assert err.line == line
    assert err.source == "<string>"
