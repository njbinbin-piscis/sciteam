"""Auditor-only evidence for SciCode problems (docs/60 §6.4). Runs as an institution validator
inside the validation sandbox: /workspace holds the committed solution, /audit holds this file
and bundle.pkl (the held-out case of every audited step with its numeric target). The members'
sandboxes never mount /audit. Output is only PASS/FAIL per step plus the exception class: no
expected values, no messages, so the evidence cannot leak the target.

Exit code 0 only if every audited step passes.
"""

import json
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

AUDIT = Path("/audit")
SOLUTION = Path("/workspace/solution.py")
TIMEOUT_S = 600
MARK = "@@AUDIT@@"

RUNNER = """\
import json, pickle, sys
sys.path.insert(0, '/audit')
target = pickle.load(open(sys.argv[1], 'rb'))['target']
_r = None
try:
{case}
except BaseException as e:
    _r = type(e).__name__
print('{mark}' + json.dumps(_r))
"""


def run_step(code: str, step: dict, tmp: Path) -> str | None:
    """None if the held-out case passes, otherwise a short reason."""
    tf = tmp / f"t_{step['sid']}.pkl"
    tf.write_bytes(pickle.dumps({"target": step["target"]}))
    body = "\n".join("    " + ln for ln in step["case"].splitlines()) or "    pass"
    script = tmp / f"s_{step['sid']}.py"
    script.write_text(code + "\n\n" + RUNNER.format(case=body, mark=MARK))
    try:
        p = subprocess.run([sys.executable, str(script), str(tf)], capture_output=True,
                           text=True, timeout=TIMEOUT_S, cwd=tmp,
                           env={"PATH": "/usr/bin:/bin:/usr/local/bin", "OMP_NUM_THREADS": "1",
                                "OPENBLAS_NUM_THREADS": "1"})
    except subprocess.TimeoutExpired:
        return "timeout"
    for ln in reversed(p.stdout.splitlines()):
        if ln.startswith(MARK):
            return json.loads(ln[len(MARK):])
    return "solution does not run (import or module-level error)"


def main() -> int:
    bundle = pickle.loads((AUDIT / "bundle.pkl").read_bytes())
    agent = SOLUTION.read_text() if SOLUTION.exists() else ""
    if not agent.strip():
        print("no solution.py")
        return 1
    # The reference code of the steps the benchmark itself provides comes last, as in grading.
    code = "\n\n".join([bundle["deps"], agent, *bundle["gold"].values()])
    failed = []
    with tempfile.TemporaryDirectory() as td:
        for step in bundle["steps"]:
            why = run_step(code, step, Path(td))
            print(f"step {step['sid']}: {'PASS' if why is None else 'FAIL (' + why + ')'}")
            if why is not None:
                failed.append(step["sid"])
    print(f"held-out check: {len(bundle['steps']) - len(failed)}/{len(bundle['steps'])} steps pass"
          + (f"; failing: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
