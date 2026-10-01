"""Spec §5 (environment overrides)."""

from confkit import loads

BASE = "[server]\nport = 80\nhost = localhost\n[server.http]\ntimeout = 5\n[Mixed]\nk = 1\n"


def test_override_is_typed_and_replaces_value():
    env = {"CONFKIT_SERVER__PORT": "9000", "PATH": "/usr/bin", "OTHER_SERVER__HOST": "nope"}
    cfg = loads(BASE, env=env)
    assert cfg.get("server.port") == 9000 and type(cfg.get("server.port")) is int
    assert cfg.get("server.host") == "localhost"


def test_section_matching_lowercases_and_replaces_dots():
    env = {"CONFKIT_SERVER_HTTP__TIMEOUT": "30", "CONFKIT_MIXED__K": "2"}
    cfg = loads(BASE, env=env)
    assert cfg.get("server.http.timeout") == 30
    assert cfg.get("Mixed.k") == 2
    assert cfg.sections() == ["server", "server.http", "Mixed"]


def test_unmatched_section_is_created_lowercased_after_file_sections():
    cfg = loads(BASE, env={"CONFKIT_NEWSEC__Some_Key": "hello"})
    assert cfg.sections() == ["server", "server.http", "Mixed", "newsec"]
    assert cfg["newsec"] == {"some_key": "hello"}


def test_name_split_at_first_double_underscore():
    cfg = loads(BASE, env={"CONFKIT_SERVER__A__B": "x"})
    assert cfg.get("server.a__b") == "x"


def test_ignored_names_env_none_and_custom_prefix():
    ignored = {"CONFKIT_NOSPLIT": "1", "CONFKIT___KEY": "1", "CONFKIT_SERVER__": "1"}
    assert loads(BASE, env=ignored) == loads(BASE)
    assert loads(BASE, env=None) == loads(BASE)
    cfg = loads(BASE, env={"APP_SERVER__PORT": "1", "CONFKIT_SERVER__HOST": "no"}, prefix="APP_")
    assert cfg.get("server.port") == 1
    assert cfg.get("server.host") == "localhost"


def test_values_are_typed_like_config_values():
    env = {
        "CONFKIT_T__Q": '"quoted # text"',
        "CONFKIT_T__S": "'single'",
        "CONFKIT_T__L": '[1, "two", false]',
        "CONFKIT_T__B": "TRUE",
        "CONFKIT_T__F": "-2.5",
        "CONFKIT_T__U": "plain # not a comment",
    }
    t = loads("", env=env)["t"]
    assert t == {
        "q": "quoted # text",
        "s": "single",
        "l": [1, "two", False],
        "b": True,
        "f": -2.5,
        "u": "plain # not a comment",
    }


def test_overrides_apply_after_merge_and_before_interpolation():
    layer1 = "[server]\nhost = a\nurl = http://${host}/\nport = ${server.p}\np = 1\n"
    layer2 = "[server]\nhost = b\n"
    env = {"CONFKIT_SERVER__HOST": "c", "CONFKIT_SERVER__P": "7", "CONFKIT_SERVER__ALIAS": "${host}-x"}
    cfg = loads(layer1, layer2, env=env)
    assert cfg.get("server.url") == "http://c/"
    assert cfg.get("server.port") == 7
    assert cfg.get("server.alias") == "c-x"
