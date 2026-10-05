"""Prepare SciCode-Verified for the experiment runner (docs/60 §4.2).

  seeds/<pid>/PROBLEM.md + solution.py   what the agents see (no targets, no reference code)
  sciteam-sci:v0                         sandbox image: scientific Python, no network, no h5
  configs/tasksets/scicode-verified.yaml  generated task set

Usage (repo root, venv active): python bench/scicode/prepare.py [--build-image] [--calibrate]
"""

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import grade  # noqa: E402

ROOT = HERE.parent.parent
SEEDS = HERE / "seeds"
UP_RUNS = HERE / "upstream" / "eval_clean" / "ds_runs"
TASKSET = ROOT / "configs" / "tasksets" / "scicode-verified.yaml"
IMAGE = "sciteam-sci:v0"
PINS = "numpy==2.2.6 scipy==1.15.3 sympy==1.13.3 matplotlib==3.10.3"   # the 2025-era grading env

DOCKERFILE = f"""\
FROM sciteam-team:py312
RUN pip install --no-cache-dir {PINS}
"""

BRIEF = (
    "Solve the scientific computing problem in /workspace/PROBLEM.md. Implement every function "
    "listed there in /workspace/solution.py, keeping each function's name and signature. I will "
    "run the solution against reference values that you cannot see, one step after another, "
    "and a step only counts if its function is numerically correct, so physical conventions, "
    "units and edge cases matter. The steps build on each other: an early mistake breaks every "
    "later step. Keep solution.py importable: no top-level computation, no printing, no plotting. "
    "The environment has numpy, scipy, sympy and matplotlib installed (no network, do not "
    "install packages). I am not available during the run; decide everything yourselves.")


def problem_md(p: dict, gold_steps: set[int]) -> str:
    out = [f"# {p['problem_name']}", "", p["problem_description_main"].strip(), "",
           "## Input / output conventions", "", p["problem_io"].strip(), "",
           "## Allowed imports (already placed at the top of solution.py)", "", "```python",
           p["required_dependencies"].strip(), "```", "", "## Steps", ""]
    for k, s in enumerate(p["sub_steps"]):
        out += [f"### Step {s['step_number']}", ""]
        if k in gold_steps:
            out += ["*This step is already provided in solution.py and is not scored; "
                    "do not change it.*", ""]
        out += [s["step_description_prompt"].strip(), "", "```python",
                s["function_header"].rstrip(), "", (s["return_line"] or "").rstrip(), "```", ""]
    return "\n".join(out)


def solution_py(p: dict, gold_steps: set[int]) -> str:
    out = [p["required_dependencies"].strip(), ""]
    for k, s in enumerate(p["sub_steps"]):
        if k in gold_steps:
            out += [f"# --- step {s['step_number']}: provided, do not change ---",
                    grade.gold(s["step_number"]).strip(), ""]
        else:
            out += [f"# --- step {s['step_number']} ---", s["function_header"].rstrip(),
                    "    raise NotImplementedError", ""]
    return "\n".join(out)


def make_seeds() -> list[dict]:
    metas = []
    for line in grade.DATA.read_text().splitlines():
        p = json.loads(line)
        pid = str(p["problem_id"])
        gold_steps = {k for k in range(len(p["sub_steps"])) if (pid, k) in grade.SKIP}
        d = SEEDS / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "PROBLEM.md").write_text(problem_md(p, gold_steps))
        (d / grade.SOLUTION).write_text(solution_py(p, gold_steps))
        scored = [s["step_number"] for k, s in enumerate(p["sub_steps"])
                  if k not in gold_steps and s["test_cases"]]
        metas.append({"pid": pid, "name": p["problem_name"], "steps": len(scored)})
    return metas


def check_dataset() -> str:
    """The grader reads the h5 on every run; verify it once against the release manifest."""
    manifest = json.loads((grade.UP / "scicode_verified" / "manifest.json").read_text())
    out = subprocess.run(["md5sum", str(grade.H5)], capture_output=True, text=True, check=True)
    md5 = out.stdout.split()[0]
    if md5 != manifest["h5_md5"]:
        raise SystemExit(f"h5 md5 {md5} != manifest {manifest['h5_md5']}")
    return md5


def write_taskset(metas: list[dict]) -> None:
    ts = {
        "name": "scicode-verified",
        "grader": ".venv/bin/python bench/scicode/grade.py {task.pid} {workspace} --json {out}",
        "vars": {"brief": BRIEF},
        "patches": {
            "runtime/default.yaml": {"sandbox": {"image": IMAGE},
                                     "budget": {"cost_usd": 100, "wall_clock_s": 21600}},
            **{f"harness/{h}.yaml": {"budget": {"cost_usd": 20, "wall_clock_s": 10800,
                                                "max_steps": 2000}}
               for h in ("team-default", "team-institution", "team-org")},
        },
        # What the interpreter digest cannot see (docs/60 §3.4).
        "identity": {
            "grader": "sha256sum bench/scicode/grade.py",
            "dataset": "md5sum bench/scicode/upstream/scicode_verified/problems_test.jsonl "
                       "bench/scicode/upstream/scicode_verified/manifest.json",
            "env_2024": "bench/scicode/envs/sci2024/bin/pip freeze",
            "env_2025": "bench/scicode/envs/sci2025/bin/pip freeze",
            "sandbox_image": "docker image inspect -f '{{.Id}}' " + IMAGE,
            "seed": "cd {task.seed} && sha256sum PROBLEM.md solution.py",
        },
        "tasks": [{"id": f"p{m['pid']}",
                   "vars": {"pid": m["pid"], "seed": str(SEEDS / m["pid"]),
                            "image": IMAGE, "name": m["name"], "steps": str(m["steps"])}}
                  for m in metas],
    }
    TASKSET.write_text("# Generated by bench/scicode/prepare.py (docs/60 §4.2); do not edit.\n"
                       + yaml.safe_dump(ts, sort_keys=False, width=100, allow_unicode=True))


def calibrate(metas: list[dict], run: str) -> list[dict]:
    """The release withholds reference code, so the grader is checked against the upstream grader
    instead: the code a model wrote step by step (already scored upstream) is joined into one
    solution.py and re-scored here; every step verdict must agree. The untouched skeleton must
    score ~0 (docs/60 §10 step 2)."""
    base = next((UP_RUNS / run).glob("cleaned_*/*"))

    def one(m: dict) -> dict:
        pid = m["pid"]
        prob = grade.problem(pid)
        up = {}
        parts = []
        for k, s in enumerate(prob["sub_steps"]):
            sid = s["step_number"]
            parts.append((base / pid / f"{sid}.code.py").read_text())
            sf = base / pid / f"{sid}.score.json"
            if sf.exists() and (pid, k) not in grade.SKIP and s["test_cases"]:
                up[sid] = json.loads(sf.read_text())["passed"]
        with tempfile.TemporaryDirectory() as td:
            ws, seed = Path(td) / "ws", Path(td) / "seed"
            ws.mkdir()
            seed.mkdir()
            (ws / grade.SOLUTION).write_text("\n\n".join(parts))
            (seed / grade.SOLUTION).write_text((SEEDS / pid / grade.SOLUTION).read_text())
            mine, empty = grade.grade(pid, ws), grade.grade(pid, seed)
        diff = [sid for sid, ok in up.items() if mine["steps"][sid]["passed"] != ok]
        row = {"pid": pid, "steps": len(up), "disagree": diff, "seed_score": empty["score"]}
        print(json.dumps(row), flush=True)
        return row
    with ThreadPoolExecutor(max_workers=16) as ex:
        rows = list(ex.map(one, metas))
    (HERE / "calibration.json").write_text(json.dumps(rows, indent=2))
    n = sum(r["steps"] for r in rows)
    bad = sum(len(r["disagree"]) for r in rows)
    print(f"steps {n}, disagreements {bad}, max seed score {max(r['seed_score'] for r in rows)}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-image", action="store_true")
    ap.add_argument("--calibrate", metavar="UPSTREAM_RUN",
                    help="validate the grader against a scored upstream run, e.g. screen-flash-off")
    a = ap.parse_args()
    print("h5 md5", check_dataset())
    metas = make_seeds()
    if a.build_image:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "Dockerfile").write_text(DOCKERFILE)
            subprocess.run(["docker", "build", "-q", "-t", IMAGE, td], check=True, timeout=1800)
    write_taskset(metas)
    print(f"{len(metas)} problems, {sum(m['steps'] for m in metas)} scored steps")
    if a.calibrate:
        calibrate(metas, a.calibrate)


if __name__ == "__main__":
    main()
