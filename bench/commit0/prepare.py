"""Prepare Commit0 libraries for SciTeam experiments (docs/60-experiment.md §4).

For each library: clone the skeleton (`commit0 setup`), build the official test image
(`commit0 build`), build a sandbox image for the agents, and write a workspace seed with the
upstream tests removed and the specification as text. Then write the task set file.

Usage (from bench/commit0, with the project venv active):
    python prepare.py tinydb simpy ...          # or: --split lite
    python prepare.py --split lite --taskset ../../configs/tasksets/commit0-lite.yaml
"""

import argparse
import bz2
import json
import shutil
import subprocess
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPOS, SEEDS, SEEDS_VISIBLE = HERE / "repos", HERE / "seeds", HERE / "seeds-visible"
DATASET = "wentingzhao/commit0_combined"

SANDBOX_DOCKERFILE = """\
FROM wentingzhao/{lib}:v0
# The official image keeps its venv inside /testbed; the agents' workspace is mounted on
# /testbed, so the venv moves out of the way and the editable install points at their code.
RUN cp -a /testbed/.venv /opt/c0venv \\
 && sed -i 's#/testbed/.venv#/opt/c0venv#g' /opt/c0venv/bin/activate \\
 && for f in /opt/c0venv/bin/*; do \\
      [ -f "$f" ] && head -c 2 "$f" | grep -q '#!' && sed -i '1s#/testbed/.venv#/opt/c0venv#' "$f"; \\
    done; true
RUN (command -v rg >/dev/null || (timeout 300 apt-get update \\
      && timeout 300 apt-get install -y --no-install-recommends ripgrep procps \\
      && rm -rf /var/lib/apt/lists/*) || true) \\
 && git config --system user.email team@sciteam && git config --system user.name sciteam \\
 && git config --system --add safe.directory '*'
# What the official /testbed holds beyond git (generated at install); the env-parity probe
# checks the agents' workspace against it.
RUN cd /testbed && git ls-files -z --others --ignored --exclude-standard \\
    | grep -zvE '{exclude}' {hidden_filter}| xargs -0 -r sha256sum > /opt/c0env.sha256
ENV VIRTUAL_ENV=/opt/c0venv PATH=/opt/c0venv/bin:$PATH
WORKDIR /testbed
"""


def sh(*cmd: str, cwd: Path = HERE) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def instances() -> dict[str, dict]:
    from datasets import load_dataset

    return {r["repo"].split("/")[-1]: r for r in load_dataset(DATASET, split="test")}


def lite() -> list[str]:
    from commit0.harness.constants import SPLIT

    return list(SPLIT["lite"])


def hidden_paths(inst: dict) -> list[str]:
    """What the agents must not see: the test directory, or the test files when the tests
    live at the repository root."""
    from commit0.harness.get_pytest_ids import main as get_tests

    test_dir = inst["test"]["test_dir"].rstrip("/")
    if test_dir not in ("", "."):
        return [test_dir]
    files = sorted({t.split("::")[0] for t in get_tests(inst["repo"].split("/")[-1], 0) if t})
    return files


EXTRAS_EXCLUDE = r"^\.venv/|__pycache__|\.pyc$|\.egg-info/|^\.pytest_cache|^build/|^\.tox/"
EXTRAS_SCRIPT = (
    "cd /testbed && git ls-files -z --others --ignored --exclude-standard "
    "| grep -zvE '{exclude}' | tar --null -T - -cf -")


def image_extras(lib: str, dest: Path, hidden: list[str]) -> int:
    """Files the official image has in /testbed that git does not track (generated at install,
    e.g. babel's CLDR locale data). The agents' workspace is mounted over /testbed, so without
    them the agents work in a different environment from the one the grader uses."""
    data = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh",
         f"wentingzhao/{lib.lower()}:v0", "-c", EXTRAS_SCRIPT.format(exclude=EXTRAS_EXCLUDE)],
        check=True, capture_output=True, timeout=1800).stdout
    n = 0
    with tarfile.open(fileobj=BytesIO(data)) as tf:
        members = [m for m in tf.getmembers()
                   if not any(m.name == h or m.name.startswith(h.rstrip("/") + "/")
                              for h in hidden)]
        tf.extractall(dest, members=members, filter="data")
        n = sum(1 for m in members if m.isfile())
    return n


def seed(lib: str, inst: dict) -> dict:
    repo = REPOS / lib
    dest = SEEDS / lib
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    data = subprocess.run(["git", "-C", str(repo), "archive", inst["base_commit"]],
                          check=True, capture_output=True).stdout
    with tarfile.open(fileobj=BytesIO(data)) as tf:
        tf.extractall(dest, filter="data")
    hidden = hidden_paths(inst)
    for rel in hidden:
        p = dest / rel
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    extras = image_extras(lib, dest, hidden)
    spec_bz2 = dest / "spec.pdf.bz2"
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "spec.pdf"
        pdf.write_bytes(bz2.decompress(spec_bz2.read_bytes()))
        subprocess.run(["pdftotext", "-layout", str(pdf), str(dest / "SPEC.txt")], check=True)
    spec_bz2.unlink()
    meta = {"lib": lib, "base_commit": inst["base_commit"], "src_dir": inst["src_dir"],
            "test_cmd": inst["test"]["test_cmd"], "test_dir": inst["test"]["test_dir"],
            "hidden": hidden, "image": f"sciteam-c0-{lib.lower()}:v0", "image_extras": extras,
            "spec_chars": len((dest / "SPEC.txt").read_text(errors="replace"))}
    (SEEDS / f"{lib}.json").write_text(json.dumps(meta, indent=2))
    return meta


def visible_seed(lib: str, meta: dict) -> Path:
    """Official protocol (tests visible): the hidden seed plus the upstream tests at base."""
    dest = SEEDS_VISIBLE / lib
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SEEDS / lib, dest)
    data = subprocess.run(["git", "-C", str(REPOS / lib), "archive", meta["base_commit"],
                           *meta["hidden"]], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=BytesIO(data)) as tf:
        tf.extractall(dest, filter="data")
    return dest


def build_images(lib: str, hidden: list[str], official: bool = True) -> None:
    if official:
        subprocess.run(["commit0", "setup", lib], cwd=HERE, check=True)
        subprocess.run(["commit0", "build"], cwd=HERE, check=True)
    hidden_filter = "".join(f"| grep -zvE '^{h.rstrip('/')}(/|$)' " for h in hidden)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "Dockerfile").write_text(SANDBOX_DOCKERFILE.format(
            lib=lib.lower(), exclude=EXTRAS_EXCLUDE, hidden_filter=hidden_filter))
        subprocess.run(["docker", "build", "-q", "-t", f"sciteam-c0-{lib.lower()}:v0", tmp],
                       check=True, timeout=1800)


BRIEF = (
    "Implement the Python library `{task.lib}` in /testbed. The repository is the real "
    "project with every function and method body removed (they are stubs); its "
    "documentation is in /testbed/SPEC.txt and in the docstrings. Keep the public API "
    "exactly as it is: module paths, names and signatures. {tests} The Python environment "
    "with all dependencies is already installed and active (no network, do not install "
    "packages). I am not available during the run; decide everything yourselves.")
TESTS_HIDDEN = ("The upstream test suite has been removed; I will judge the result with it, "
                "so every documented behaviour matters, including errors and edge cases.")
TESTS_VISIBLE = ("The upstream test suite is included ({test_dir}); I will judge the result "
                 "with the original version of it, so changing tests does not help.")


def write_taskset(path: Path, name: str, metas: list[dict], visible: bool = False) -> None:
    tests = TESTS_VISIBLE.format(test_dir="{task.test_dir}") if visible else TESTS_HIDDEN
    seeds = SEEDS_VISIBLE if visible else SEEDS
    ts = {
        "name": name,
        "grader": ".venv/bin/python bench/commit0/grade.py {task.lib} {workspace} --json {out}",
        "vars": {"brief": BRIEF.replace("{tests}", tests)},
        # Breakers stay out of reach of normal work (docs/00 §3): a library is far larger
        # than minipkg, so every arm gets the same wider limits.
        "patches": {
            "runtime/default.yaml": {"sandbox": {"image": "{task.image}",
                                                 "workspace_mount": "/testbed"},
                                     "budget": {"cost_usd": 200, "wall_clock_s": 43200}},
            **{f"harness/{h}.yaml": {"budget": {"cost_usd": 50, "wall_clock_s": 21600,
                                                "max_steps": 4000}}
               for h in ("team-default", "team-institution", "team-org")},
            # The certificate checks what the work needs: the library's own environment and
            # every file the official environment has beyond git (docs/60 §4.1).
            "institution/genesis-rules.yaml": {
                "validators": {
                    "env-python": {"command": "python -c 'import sys; assert sys.prefix == "
                                              "\"/opt/c0venv\", sys.prefix'"},
                    "env-parity": {"command": "cd /testbed && (test ! -s /opt/c0env.sha256 "
                                              "|| sha256sum -c --quiet /opt/c0env.sha256)",
                                   "timeout_s": 600}},
                "environments": {"workspace": {
                    "probes": ["env-python", "env-pytest", "env-parity"]}}},
        },
        "tasks": [{"id": m["lib"], "vars": {"lib": m["lib"], "seed": str(seeds / m["lib"]),
                                              "image": m["image"], "test_dir": m["test_dir"]}}
                  for m in metas],
    }
    path.write_text("# Generated by bench/commit0/prepare.py (docs/60 §4); do not edit by hand.\n"
                    + yaml.safe_dump(ts, sort_keys=False, width=100, allow_unicode=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("libs", nargs="*")
    ap.add_argument("--split", choices=["lite"])
    ap.add_argument("--taskset", type=Path)
    ap.add_argument("--name", default="commit0-lite")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--sandbox-only", action="store_true",
                    help="rebuild only the agents' sandbox images (official images exist)")
    ap.add_argument("--visible-taskset", type=Path, help="also write the tests-visible set")
    a = ap.parse_args()
    libs = a.libs or (lite() if a.split == "lite" else [])
    if not libs:
        ap.error("give libraries or --split")
    insts = instances()
    metas = []
    for lib in libs:
        if not a.no_build:
            build_images(lib, hidden_paths(insts[lib]), official=not a.sandbox_only)
        metas.append(seed(lib, insts[lib]))
        print(json.dumps(metas[-1]))
    if a.taskset:
        write_taskset(a.taskset, a.name, metas)
    if a.visible_taskset:
        for m in metas:
            visible_seed(m["lib"], m)
        write_taskset(a.visible_taskset, a.name + "-visible", metas, visible=True)


if __name__ == "__main__":
    main()
