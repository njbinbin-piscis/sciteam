"""``sciteam`` command-line entry point.

Deliberately thin: every subcommand is a few lines over a function the
library already exposes and tests already cover. The CLI adds no behaviour
of its own — in particular it never bypasses the institution layer (there is
no ``--force`` anywhere here, by design).

    sciteam demo [--mode full|prompt_only|no_separation|no_recovery] [--config-swap]
    sciteam validate <manifest.yaml | dir> [...]
    sciteam plugins list [--strict]
    sciteam paradigms list
    sciteam ledger verify|show|replay|cost <ledger.jsonl | run dir>
    sciteam mutate <manifest.yaml | dir> [-v]
    sciteam env [--against <run dir>]
    sciteam version
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from sciteam import __version__, canonical_demo
from sciteam._paths import ASSETS_ROOT, DATA_ROOT
from sciteam.checkpoint import describe as describe_resume
from sciteam.checkpoint import read_resume_state
from sciteam.event_log import (
    LEDGER_FILENAME,
    LedgerError,
    cost_report,
    read_events,
    replay,
    verify_chain,
)
from sciteam.ledger_view import export_html_view
from sciteam.mutation import mutate_file
from sciteam.plugin_api import PLUGIN_API_VERSION, load_all_plugins
from sciteam.provenance import compare, environment_fingerprint
from sciteam.scaffold import list_templates, scaffold_project
from sciteam.team_loader import load_team_config, load_team_configs

BUILTIN_PARADIGMS_DIR = ASSETS_ROOT / "teams" / "paradigms"


# --- validate ------------------------------------------------------------------


def _iter_manifest_files(paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.yaml")))
        else:
            files.append(p)
    return files


def cmd_validate(args: argparse.Namespace) -> int:
    """Run the load-time institutional invariants on one or more manifests.

    This is exactly what ``build_registry`` does before any campaign starts
    (authority mutual-exclusion, ``judgment`` stopping needs an assessor,
    roster policy, ...). Exit code is the number of failing manifests, capped
    at 1 for shells — so ``sciteam validate my.yaml && ...`` reads naturally.
    """
    files = _iter_manifest_files(args.paths)
    if not files:
        print("validate: no manifest files found", file=sys.stderr)
        return 2
    failures = 0
    for path in files:
        try:
            cfg = load_team_config(path)
        except (ValueError, OSError) as exc:
            failures += 1
            print(f"FAIL  {path}\n      {exc}")
            continue
        seats = ", ".join(
            m.agent_key
            + ("*" if m.emits_exit_artifact else "")
            + (f"[{','.join(m.authority)}]" if m.authority else "")
            for m in cfg.members
        )
        print(f"ok    {path}  ({cfg.team_id}: {seats})")
    if failures:
        print(f"\n{failures} of {len(files)} manifest(s) failed validation", file=sys.stderr)
    return 1 if failures else 0


# --- plugins --------------------------------------------------------------------


def cmd_plugins_list(args: argparse.Namespace) -> int:
    registry = load_all_plugins(strict=args.strict)
    plugins = registry.loaded_plugins
    if args.json:
        print(
            json.dumps(
                {
                    "plugin_api_version": PLUGIN_API_VERSION,
                    "plugins": [
                        {
                            "id": m.id,
                            "version": m.version,
                            "sciteam_api_version": m.sciteam_api_version,
                            "provides": list(m.provides),
                            "description": m.description,
                        }
                        for m in plugins
                    ],
                    "tools": [spec.name for spec in registry.all_tool_specs()],
                    "paradigm_dirs": [str(p) for p in registry.paradigm_dirs],
                },
                indent=2,
            )
        )
        return 0
    print(f"plugin API {PLUGIN_API_VERSION}; {len(plugins)} plugin(s) loaded")
    for m in plugins:
        provides = f"  provides={','.join(m.provides)}" if m.provides else ""
        print(f"  {m.id} {m.version}  (api {m.sciteam_api_version}){provides}")
    tools = registry.all_tool_specs()
    if tools:
        print("  tools: " + ", ".join(spec.name for spec in tools))
    return 0


def cmd_plugins_add(args: argparse.Namespace) -> int:
    package = args.package
    print(f"installing plugin {package}...")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"FAILED to install {package}:\n{proc.stderr}", file=sys.stderr)
        return proc.returncode

    try:
        registry = load_all_plugins(strict=True)
        plugins = registry.loaded_plugins
        print(f"successfully installed {package}; {len(plugins)} plugin(s) active")
        return 0
    except Exception as exc:
        print(f"WARNING: package installed but plugin verification failed: {exc}", file=sys.stderr)
        return 1


def cmd_plugins_remove(args: argparse.Namespace) -> int:
    package = args.package
    print(f"uninstalling plugin {package}...")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", package],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"FAILED to uninstall {package}:\n{proc.stderr}", file=sys.stderr)
        return proc.returncode
    print(f"successfully uninstalled {package}")
    return 0


# --- paradigms ------------------------------------------------------------------


def cmd_paradigms_list(args: argparse.Namespace) -> int:
    extra = load_all_plugins().paradigm_dirs if args.with_plugins else ()
    configs = load_team_configs(BUILTIN_PARADIGMS_DIR, extra_roots=extra)
    for cfg in configs:
        coord = cfg.coordination or {}
        topo = coord.get("topology", "?")
        print(f"{cfg.team_id:28s} {len(cfg.members):2d} seats  topology={topo}  {cfg.name}")
    return 0


# --- ledger ---------------------------------------------------------------------


def _ledger_path(raw: str) -> Path:
    path = Path(raw)
    return path / LEDGER_FILENAME if path.is_dir() else path


def cmd_ledger_verify(args: argparse.Namespace) -> int:
    """Re-derive the hash chain and report every break, not just the first.

    Exit code 1 on a broken chain, so this drops straight into CI as the
    guard that a run's record was not edited after the fact.
    """
    path = _ledger_path(args.path)
    result = verify_chain(path)
    if args.json:
        print(
            json.dumps(
                {
                    "path": str(path),
                    "ok": result.ok,
                    "length": result.length,
                    "head": result.head,
                    "problems": list(result.problems),
                },
                indent=2,
            )
        )
    elif result.ok:
        print(f"ok    {path}  {result.length} entries, head {result.head[:16]}…")
    else:
        print(f"BROKEN {path}  {len(result.problems)} problem(s)")
        for problem in result.problems:
            print(f"  - {problem}")
    return 0 if result.ok else 1


def cmd_ledger_show(args: argparse.Namespace) -> int:
    path = _ledger_path(args.path)
    try:
        events = read_events(path)
    except LedgerError as exc:
        print(f"ledger show: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([e.to_dict() for e in events], indent=2, ensure_ascii=False))
        return 0
    if not events:
        print(f"ledger show: {path} is empty")
        return 0
    for event in events:
        marker = {"accepted": "+", "rejected": "x", "observed": " "}[event.effect]
        seat = event.seat or "-"
        authority = f" [{event.authority}]" if event.authority else ""
        print(f"{event.seq:>4} {marker} {event.kind:<20} {seat:<14}{authority}")
    return 0


def cmd_ledger_replay(args: argparse.Namespace) -> int:
    """Fold the chain into the institutional state it implies."""
    path = _ledger_path(args.path)
    state = replay(path)
    print(
        json.dumps(
            {
                "run_id": state.run_id,
                "rule_versions": state.rule_versions,
                "effective_artifacts": state.effective_artifacts,
                "refused_acts": state.refused_acts,
                "vetoes": state.vetoes,
                "reopenings": state.reopenings,
                "closed": state.closed,
                "amendments": state.amendments,
                "finished": state.finished,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_ledger_cost(args: argparse.Namespace) -> int:
    """Attribute spend to the seat that incurred it."""
    lines = cost_report(_ledger_path(args.path))
    if args.json:
        print(json.dumps([line.__dict__ for line in lines], indent=2))
        return 0
    if not lines:
        print("no llm_call entries in this ledger")
        return 0
    print(f"{'seat':<18}{'model':<24}{'calls':>7}{'prompt':>10}{'completion':>12}{'usd':>10}")
    for line in lines:
        print(
            f"{line.seat:<18}{line.model:<24}{line.calls:>7}"
            f"{line.prompt_tokens:>10}{line.completion_tokens:>12}{line.usd:>10.4f}"
        )
    calls = sum(line.calls for line in lines)
    usd = sum(line.usd for line in lines)
    print(f"{'TOTAL':<42}{calls:>7}{'':>22}{usd:>10.4f}")
    return 0


def cmd_ledger_resume_state(args: argparse.Namespace) -> int:
    """Report what an interrupted run already decided, before resuming it.

    Resuming re-runs only the missions with no decision on record, so this
    is the answer to "what will actually happen if I restart it?".
    """
    state = read_resume_state(_ledger_path(args.path))
    summary = describe_resume(state)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    if not state.run_id:
        print("no run recorded in this ledger")
        return 0
    status = "finished" if state.finished else "interrupted"
    print(f"run {state.run_id} ({status})")
    print(f"  decided missions  {', '.join(summary['decided_missions']) or '(none)'}")
    print(f"  of which succeeded {summary['succeeded']}")
    print(f"  tokens charged    {summary['tokens_charged']}")
    if state.finished:
        print("  resuming would run nothing: this run already completed")
    return 0


def cmd_ledger_view(args: argparse.Namespace) -> int:
    """Export or view the ledger as an interactive, standalone HTML file."""
    import webbrowser

    dest = export_html_view(_ledger_path(args.path), out_path=args.out)
    print(f"exported interactive ledger view to: {dest}")
    if args.open:
        webbrowser.open(dest.as_uri())
    return 0


# --- mutate ---------------------------------------------------------------------


def cmd_mutate(args: argparse.Namespace) -> int:
    """Break each institution on purpose and report what loaded anyway.

    Exit 1 on any survivor (a broken institution that was accepted) or any
    false kill (a harmless edit that was rejected). Both are defects: the
    first means a guard is missing, the second means the loader refuses
    edits rather than illegitimate institutions.
    """
    files = _iter_manifest_files(args.paths)
    if not files:
        print("mutate: no manifest files found", file=sys.stderr)
        return 2
    reports = []
    for path in files:
        try:
            reports.append(mutate_file(path))
        except (ValueError, OSError) as exc:
            print(f"mutate: cannot read {path}: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "source": r.source,
                        "score": r.score,
                        "results": [asdict(res) for res in r.results],
                    }
                    for r in reports
                ],
                indent=2,
            )
        )
        return 0 if all(r.ok for r in reports) else 1

    defects = 0
    for report in reports:
        flag = "ok   " if report.ok else "GAP  "
        print(f"{flag} {report.source}  score={report.score:.2f}")
        if args.verbose:
            for res in report.results:
                state = "n/a" if not res.applicable else ("killed" if res.killed else "survived")
                print(f"        {res.operator:<22} {state:<9} {res.description}")
        for res in report.survivors:
            defects += 1
            print(f"        SURVIVED  {res.operator}: {res.description}")
            print(f"                  threatens: {res.threatens}")
        for res in report.false_kills:
            defects += 1
            print(f"        FALSE KILL {res.operator}: {res.description}")
            print(f"                  rejected with: {res.detail}")
    if defects:
        print(f"\n{defects} defect(s) across {len(reports)} manifest(s)", file=sys.stderr)
    return 1 if defects else 0


# --- env ------------------------------------------------------------------------


def cmd_env(args: argparse.Namespace) -> int:
    """Print this environment's provenance fingerprint.

    With ``--against <run dir | summary.json>``, diff it against the
    environment a previous run recorded: the useful answer when a result
    fails to reproduce is *which* covariate moved.
    """
    current = environment_fingerprint()
    if args.against:
        path = Path(args.against)
        if path.is_dir():
            path = path / "summary.json"
        try:
            recorded = json.loads(path.read_text(encoding="utf-8")).get("provenance")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"env: cannot read provenance from {path}: {exc}", file=sys.stderr)
            return 2
        if not recorded:
            print(f"env: {path} records no provenance", file=sys.stderr)
            return 2
        diffs = compare(recorded, current)
        if args.json:
            print(json.dumps({"differences": diffs}, indent=2))
        elif not diffs:
            print(f"identical pinned environment ({current['pinned_digest'][:16]}…)")
        else:
            print(f"{len(diffs)} difference(s) from {path}:")
            for line in diffs:
                print(f"  - {line}")
        return 0 if not diffs else 1

    if args.json:
        print(json.dumps(current, indent=2))
        return 0
    pinned = current["pinned"]
    print(f"sciteam      {pinned['sciteam_version']}  ({pinned['sciteam_build']})")
    print(f"python       {pinned['python']['version']} {pinned['python']['implementation']}")
    plat = pinned["platform"]
    print(f"platform     {plat['system']} {plat['release']} {plat['machine']}")
    deps = ", ".join(f"{k} {v}" for k, v in pinned["dependencies"].items())
    print(f"dependencies {deps}")
    print(f"plugins      {', '.join(pinned['plugins']) or '(none)'}")
    if pinned["env_overrides"]:
        print("env overrides")
        for name, value in pinned["env_overrides"].items():
            print(f"  {name}={value}")
    print(f"digest       {current['pinned_digest']}")
    if current["unpinnable"]:
        print(f"\nNOT reproducible — {len(current['unpinnable'])} uncontrolled variable(s):")
        for gap in current["unpinnable"]:
            print(f"  - {gap['what']}: {gap['why']}")
    else:
        print("\nfully pinned")
    return 0


# --- version --------------------------------------------------------------------


def cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        print(
            json.dumps(
                {
                    "sciteam": __version__,
                    "plugin_api_version": PLUGIN_API_VERSION,
                    "data_root": str(DATA_ROOT),
                }
            )
        )
    else:
        print(f"sciteam {__version__} (plugin API {PLUGIN_API_VERSION})")
    return 0


# --- init -----------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a new institutional project from a paradigm template."""
    if args.list:
        templates = list_templates()
        if args.json:
            print(json.dumps([asdict(t) for t in templates], indent=2))
            return 0
        print(f"{'TEMPLATE':<28}{'TOPOLOGY':<14}{'SEATS':>6}  {'DESCRIPTION'}")
        print("-" * 80)
        for t in templates:
            desc = t.description[:40] + ("..." if len(t.description) > 40 else "")
            print(f"{t.id:<28}{t.topology:<14}{t.members_count:>6}  {desc}")
        return 0

    if not args.template:
        print("error: template required (or run 'sciteam init --list')", file=sys.stderr)
        return 2

    target = args.target or f"./{args.template}"
    try:
        summary = scaffold_project(
            args.template,
            target,
            project_name=args.name,
            overwrite=args.overwrite,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Initialized institutional project from '{args.template}':")
    print(f"  directory       {summary['target_dir']}")
    print(f"  manifest        {summary['manifest']}")
    print(f"  prompts ({len(summary['prompts'])})    {', '.join(summary['prompts'])}")
    print(f"  tests           {summary['test_file']}")
    print(f"  mutation score  {summary['mutation_score']:.1f} (ok={summary['mutation_ok']})")
    print("\nNext steps:")
    print(f"  cd {target}")
    print("  sciteam validate institution.yaml")
    print("  pytest test_institution.py")
    return 0


# --- parser ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sciteam",
        description="SciTeam — reference runtime for Institution Engineering.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser(
        "demo",
        help="run the paper's canonical institution run (scripted seats, no API key)",
        description=canonical_demo.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    canonical_demo.build_parser(demo)
    demo.set_defaults(func=canonical_demo.run)

    validate = sub.add_parser(
        "validate", help="check institution manifests against load-time invariants"
    )
    validate.add_argument("paths", nargs="+", help="manifest .yaml files or directories")
    validate.set_defaults(func=cmd_validate)

    plugins = sub.add_parser("plugins", help="inspect installed sciteam plugins")
    plugins_sub = plugins.add_subparsers(dest="plugins_command", required=True)
    plist = plugins_sub.add_parser("list", help="list discovered plugins")
    plist.add_argument(
        "--strict", action="store_true", help="fail on the first broken/incompatible plugin"
    )
    plist.add_argument("--json", action="store_true")
    plist.set_defaults(func=cmd_plugins_list)

    padd = plugins_sub.add_parser(
        "add", help="install a plugin package via pip and verify its manifest"
    )
    padd.add_argument(
        "package", help="package name, wheel, or path (e.g. sciteam-runtime-anthropic)"
    )
    padd.set_defaults(func=cmd_plugins_add)

    prem = plugins_sub.add_parser("remove", help="uninstall a plugin package via pip")
    prem.add_argument("package", help="package name to uninstall")
    prem.set_defaults(func=cmd_plugins_remove)

    paradigms = sub.add_parser("paradigms", help="inspect coordination paradigm templates")
    paradigms_sub = paradigms.add_subparsers(dest="paradigms_command", required=True)
    palist = paradigms_sub.add_parser("list", help="list bundled (and plugin) paradigms")
    palist.add_argument(
        "--with-plugins", action="store_true", help="include paradigm dirs registered by plugins"
    )
    palist.set_defaults(func=cmd_paradigms_list)

    ledger = sub.add_parser("ledger", help="inspect and verify an institutional ledger")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    for name, help_text, func in (
        ("verify", "re-derive the hash chain and report any tampering", cmd_ledger_verify),
        ("resume-state", "what an interrupted run already decided", cmd_ledger_resume_state),
        ("show", "list the chain entry by entry", cmd_ledger_show),
        ("replay", "fold the chain into the institutional state it implies", cmd_ledger_replay),
        ("cost", "attribute per-call spend to seats", cmd_ledger_cost),
    ):
        p = ledger_sub.add_parser(name, help=help_text)
        p.add_argument("path", help=f"a {LEDGER_FILENAME} file, or a run directory containing one")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)

    p_view = ledger_sub.add_parser(
        "view", help="export an interactive, zero-dependency HTML trajectory"
    )
    p_view.add_argument("path", help=f"a {LEDGER_FILENAME} file, or a run directory containing one")
    p_view.add_argument(
        "--out", "-o", help="destination HTML path (default: <dir>/ledger_view.html)"
    )
    p_view.add_argument("--open", action="store_true", help="open in default web browser")
    p_view.set_defaults(func=cmd_ledger_view)

    init = sub.add_parser(
        "init",
        help="scaffold a new institutional project from a paradigm template",
    )
    init.add_argument(
        "template", nargs="?", help="template ID (e.g. pipeline_chain, approval_gate)"
    )
    init.add_argument("--target", "-t", help="target directory (default: ./<template>)")
    init.add_argument("--name", "-n", help="custom project/institution name")
    init.add_argument("--list", "-l", action="store_true", help="list available paradigm templates")
    init.add_argument(
        "--overwrite", action="store_true", help="overwrite existing files in directory"
    )
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_init)

    mutate = sub.add_parser(
        "mutate", help="break institutions on purpose and check the loader notices"
    )
    mutate.add_argument("paths", nargs="+", help="manifest .yaml files or directories")
    mutate.add_argument("-v", "--verbose", action="store_true", help="show every operator")
    mutate.add_argument("--json", action="store_true")
    mutate.set_defaults(func=cmd_mutate)

    env = sub.add_parser("env", help="print this environment's provenance fingerprint")
    env.add_argument(
        "--against",
        metavar="RUN",
        help="a run directory or summary.json to diff this environment against",
    )
    env.add_argument("--json", action="store_true")
    env.set_defaults(func=cmd_env)

    version = sub.add_parser("version", help="print version information")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
