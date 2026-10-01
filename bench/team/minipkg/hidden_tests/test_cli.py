"""Spec §8 (command line), run as `python -m confkit` in a subprocess."""

import json

from confkit import loads

BASE = "[app]\nname = demo app\nport = 8080\ndebug = true\nratio = 0.75\ntags = [1, 'x']\n"


def test_get_scalars(cli, write):
    write("a.conf", BASE)
    expected = {"app.name": "demo app", "app.port": "8080", "app.debug": "true", "app.ratio": "0.75"}
    for key, out in expected.items():
        result = cli(["get", "a.conf", key])
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == out


def test_get_list_as_json_with_layered_files(cli, write):
    write("a.conf", BASE)
    write("b.conf", '[app]\ntags += ["y", true]\nport = 1\n')
    result = cli(["get", "a.conf", "b.conf", "app.tags"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1, "x", "y", True]
    assert cli(["get", "a.conf", "b.conf", "app.port"]).stdout.strip() == "1"


def test_get_missing_key_exits_2(cli, write):
    write("a.conf", BASE)
    result = cli(["get", "a.conf", "app.nope"])
    assert result.returncode == 2
    assert result.stderr.strip()


def test_environment_overrides_apply(cli, write):
    write("a.conf", BASE)
    result = cli(["get", "a.conf", "app.port"], env_extra={"CONFKIT_APP__PORT": "9999"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9999"


def test_dump_round_trips(cli, write):
    text = BASE + '[other]\ns = "a $$ b"\nref = ${app.port}\n'
    write("a.conf", text)
    result = cli(["dump", "a.conf"], env_extra={"CONFKIT_OTHER__EXTRA": "e"})
    assert result.returncode == 0, result.stderr
    expected = loads(text, env={"CONFKIT_OTHER__EXTRA": "e"})
    assert loads(result.stdout) == expected
    assert result.stdout.strip() == expected.dumps().strip()


def test_dump_json(cli, write):
    write("a.conf", BASE)
    write("b.conf", "[zzz]\nk = 1\n[app]\nport = 1\n")
    result = cli(["dump", "a.conf", "b.conf", "--json"])
    assert result.returncode == 0, result.stderr
    expected = loads(BASE, "[zzz]\nk = 1\n[app]\nport = 1\n").to_dict()
    assert json.loads(result.stdout) == expected
    assert result.stdout.strip() == json.dumps(expected, sort_keys=True, indent=2)


def test_validate(cli, write):
    write("a.conf", BASE)
    schema = {"app": {"port": {"type": "int", "max": 9000}, "name": {"type": "str"}}}
    write("ok.json", json.dumps(schema))
    ok = cli(["validate", "a.conf", "--schema", "ok.json"])
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "ok"

    bad_schema = {
        "app": {
            "__strict__": True,
            "port": {"type": "int", "max": 100},
            "name": {"type": "int"},
            "debug": {"type": "bool"},
            "ratio": {"type": "float"},
        }
    }
    write("bad.json", json.dumps(bad_schema))
    bad = cli(["validate", "a.conf", "--schema", "bad.json"])
    assert bad.returncode == 1
    lines = [line for line in (bad.stdout + "\n" + bad.stderr).splitlines() if line.strip()]
    for prefix in ("app.port: ", "app.name: ", "app.tags: "):
        assert any(line.startswith(prefix) for line in lines)
    assert not any(line.startswith(("app.debug: ", "app.ratio: ")) for line in lines)
    assert "ok" not in bad.stdout.split()


def test_errors_are_reported_on_stderr(cli, write):
    write("bad.conf", "[s]\nx = 1\n[broken\n")
    result = cli(["get", "bad.conf", "s.x"])
    assert result.returncode == 1
    assert result.stderr.strip().startswith("error: bad.conf:3: ")
    write("interp.conf", "[s]\nx = ${missing}\n")
    result = cli(["dump", "interp.conf"])
    assert result.returncode == 1
    assert result.stderr.strip().startswith("error: ")
    write("base.conf", "[s]\nx = 1\n")
    write("more.conf", "[s]\nx += 2\n")
    result = cli(["dump", "base.conf", "more.conf"])
    assert result.returncode == 1
    assert result.stderr.strip().startswith("error: ")
