"""Command line interface: python -m confkit get|dump|validate ..."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import ConfkitError, ValidationError, load

_MISSING = object()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m confkit")
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("get", help="print one value")
    get.add_argument("args", nargs="+", metavar="FILE... KEY")
    dump = sub.add_parser("dump", help="print the merged configuration")
    dump.add_argument("files", nargs="+", metavar="FILE")
    dump.add_argument("--json", action="store_true")
    val = sub.add_parser("validate", help="validate against a JSON schema")
    val.add_argument("files", nargs="+", metavar="FILE")
    val.add_argument("--schema", required=True)
    return parser


def _format(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def _run(args: argparse.Namespace) -> int:
    env = os.environ
    if args.command == "get":
        if len(args.args) < 2:
            print("error: get needs at least one FILE and a KEY", file=sys.stderr)
            return 2
        *files, key = args.args
        value = load(*files, env=env).get(key, _MISSING)
        if value is _MISSING:
            print(f"error: key {key!r} not found", file=sys.stderr)
            return 2
        print(_format(value))
        return 0
    if args.command == "dump":
        config = load(*args.files, env=env)
        if args.json:
            print(json.dumps(config.to_dict(), sort_keys=True, indent=2))
        else:
            sys.stdout.write(config.dumps())
        return 0
    with open(args.schema, encoding="utf-8") as fh:
        schema = json.load(fh)
    try:
        load(*args.files, env=env, schema=schema)
    except ValidationError as exc:
        for message in exc.errors:
            print(message)
        return 1
    print("ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except ConfkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
