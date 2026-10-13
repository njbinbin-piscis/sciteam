import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main(argv: list[str] | None = None) -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="sciteam")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("trace", help="read back activations of a run")
    t.add_argument("run_dir", type=Path)
    t.add_argument("stream", nargs="?")
    t.add_argument("--full", action="store_true")
    t.add_argument("--verify", action="store_true")

    s = sub.add_parser("swebench", help="run SWE-bench instances")
    s.add_argument("run_dir", type=Path)
    s.add_argument("--dataset", type=Path, default=ROOT / "bench/swebench/verified.jsonl")
    s.add_argument("--ids", nargs="*")
    s.add_argument("--ids-file", type=Path)
    s.add_argument("--harness", default="harness/coder-default.yaml")
    s.add_argument("--model")
    s.add_argument("--concurrency", type=int, default=8)
    s.add_argument("--pull-concurrency", type=int, default=2)
    s.add_argument("--min-free-gb", type=float, default=50,
                   help="stop starting instances when the run dir or docker root has less free")
    s.add_argument("--configs", type=Path, default=ROOT / "configs")

    e = sub.add_parser("swebench-eval", help="score a run with the official harness")
    e.add_argument("run_dir", type=Path)
    e.add_argument("--dataset", type=Path, default=ROOT / "bench/swebench/verified.jsonl")
    e.add_argument("--workers", type=int, default=16)

    tm = sub.add_parser("team", help="team runs (docs/31-runtime.md §9)")
    tsub = tm.add_subparsers(dest="team_cmd", required=True)
    ts = tsub.add_parser("start", help="create a run and drive it")
    ts.add_argument("team", help="team file relative to configs, e.g. teams/minipkg-flat.yaml")
    ts.add_argument("--run", type=Path, required=True)
    ts.add_argument("--goal")
    ts.add_argument("--configs", type=Path, default=ROOT / "configs")
    ts.add_argument("--overlay", type=Path, action="append", default=[],
                    help="config overlay dir (files there replace same-path files in configs)")
    for name in ("resume", "status", "stop"):
        tsub.add_parser(name).add_argument("run", type=Path)
    tp = tsub.add_parser("post", help="send a message as a human member")
    tp.add_argument("run", type=Path)
    tp.add_argument("--as", dest="as_", required=True)
    tp.add_argument("--to", required=True)
    tp.add_argument("text")
    tl = tsub.add_parser("log", help="chronological communication log")
    tl.add_argument("run", type=Path)
    tl.add_argument("--denials", action="store_true")
    tl.add_argument("--width", type=int, default=240)
    td = tsub.add_parser("decide", help="approve or reject what awaits a human's approval")
    td.add_argument("run", type=Path)
    td.add_argument("--as", dest="as_", required=True)
    td.add_argument("id", help="act or amendment id (see team status)")
    td.add_argument("decision", choices=["approve", "reject"])
    td.add_argument("--note", default="")
    tr = tsub.add_parser("reset", help="clear a breaker and/or a faulted agent")
    tr.add_argument("run", type=Path)
    tr.add_argument("--breaker", action="store_true")
    tr.add_argument("--agent")

    x = sub.add_parser("exp", help="experiments (docs/60-experiment.md)")
    xsub = x.add_subparsers(dest="exp_cmd", required=True)
    xr = xsub.add_parser("run", help="run every arm × task × repeat of a spec")
    xo = xsub.add_parser("one", help="run one arm/task/repeat (used by run)")
    xr.add_argument("--poll-s", type=float, default=600.0,
                    help="seconds between provider probes while paused")
    xr.add_argument("--max-pause-h", type=float, default=48.0,
                    help="give up after this many hours of waiting for the provider account")
    for p in (xr, xo):
        p.add_argument("spec", help="experiment spec relative to configs")
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--configs", type=Path, default=ROOT / "configs")
    xo.add_argument("--arm", required=True)
    xo.add_argument("--task", required=True)
    xo.add_argument("--r", type=int, required=True)
    xsub.add_parser("report", help="summarize an experiment").add_argument("out", type=Path)
    xc = xsub.add_parser("carry", help="export a run's enacted configuration as an overlay")
    xc.add_argument("run", type=Path)
    xc.add_argument("--to", type=Path, required=True)

    a = ap.parse_args(argv)
    if a.cmd == "trace":
        from sciteam.cli.trace import trace

        trace(a.run_dir, a.stream, full=a.full, verify=a.verify)
    elif a.cmd == "swebench":
        from sciteam.bench.swebench import run_swebench

        ids = list(a.ids or [])
        if a.ids_file:
            ids += [x.strip() for x in a.ids_file.read_text().splitlines() if x.strip()]
        asyncio.run(run_swebench(dataset=a.dataset, ids=ids or None, harness_cfg=a.harness,
                                 run_dir=a.run_dir, configs_dir=a.configs,
                                 concurrency=a.concurrency, model=a.model,
                                 pull_concurrency=a.pull_concurrency,
                                 min_free_gb=a.min_free_gb))
    elif a.cmd == "team":
        from sciteam.institution.team import decide
        from sciteam.org import team as inst_team
        from sciteam.runtime import team

        if a.team_cmd == "start":
            opener = inst_team.opener_for(a.configs, a.team, a.overlay)
            team.create_run(a.run, team=a.team, configs_dir=a.configs, goal=a.goal,
                            overlays=a.overlay, opener=opener)
            print(asyncio.run(team.run_team(a.run, opener=inst_team.open_run)))
        elif a.team_cmd == "resume":
            try:
                print(asyncio.run(team.run_team(a.run, opener=inst_team.open_run)))
            except team.RunLocked as e:
                raise SystemExit(f"error: {e}") from None
        elif a.team_cmd == "status":
            print(team.status(a.run, opener=inst_team.open_run))
        elif a.team_cmd == "stop":
            team.request_stop(a.run)
        elif a.team_cmd == "post":
            print(team.post(a.run, as_=a.as_, to=a.to, text=a.text, opener=inst_team.open_run))
        elif a.team_cmd == "decide":
            print(decide(a.run, as_=a.as_, id=a.id, decision=a.decision, note=a.note,
                         opener=inst_team.open_run))
        elif a.team_cmd == "log":
            from sciteam.runtime.log import team_log

            print(team_log(a.run, width=a.width, denials_only=a.denials))
        elif a.team_cmd == "reset":
            team.reset(a.run, breaker=a.breaker, agent=a.agent)
    elif a.cmd == "exp":
        from sciteam.experiment import runner

        if a.exp_cmd == "run":
            runner.run_all(a.configs, a.spec, a.out, poll_s=a.poll_s,
                           max_pause_s=a.max_pause_h * 3600.0)
            from sciteam.experiment.report import render_report, summarize

            print(render_report(summarize(a.out)))
        elif a.exp_cmd == "one":
            key = runner.RunKey(a.arm, a.task, a.r)
            print(json.dumps(runner.run_one(a.configs, a.spec, a.out, key), indent=2))
        elif a.exp_cmd == "report":
            from sciteam.experiment.report import render_report, summarize

            s = summarize(a.out)
            (a.out / "report.json").write_text(json.dumps(s, indent=2))
            print(render_report(s))
        elif a.exp_cmd == "carry":
            from sciteam.experiment.carry import carry

            print(json.dumps(carry(a.run, a.to), indent=2))
    elif a.cmd == "swebench-eval":
        run_dir = a.run_dir.resolve()
        preds = run_dir / "preds.jsonl"
        cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
               "--dataset_name", str(a.dataset.resolve()), "--predictions_path", str(preds),
               "--max_workers", str(a.workers), "--run_id", run_dir.name,
               "--cache_level", "instance", "--report_dir", str(run_dir)]
        raise SystemExit(subprocess.call(cmd, cwd=run_dir))


if __name__ == "__main__":
    main()
