"""X-evid validator (docs/60-experiment.md §6.6 v2): do two independent solutions agree on every
checkable case of a SciCode problem?

Runs as an institution validator. It reads an inputs-only bundle (built by bench/xevid/bundle.py,
which never reads the released numeric targets) and two solution files. Each solution is run on
the bundle's inputs; then the official test assertion compares A's value with B's value, B's value
standing in for the target. Solutions are never compared with the released answers, so the check
cannot leak them, and it cannot tell that both are wrong in the same way.

Standard library only: it is copied into the workspace seed that the validation sandbox runs.

A case passes only if both solutions produce a value and the assertion holds. A crash, a missing
value or a timeout counts as disagreement. Exit code 0 only if every bundled case agrees.

Usage: agree.py BUNDLE.json SOLUTION_A.py SOLUTION_B.py [--timeout S] [--python PY]
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT_S = 1800                 # = bench/scicode/grade.py TIMEOUT_S; one solution takes minutes
MARK = "@@AGREE@@"

# SciCode bundles assume upstream's `scicode.compare.cmp` helper and SciPy <1.14 `simps`/`trapz`
# aliases, neither of which is installed inside `sciteam-sci:v0` (docs/60 §11 D19).
SHIM = """import sys as _sys, types as _types
_np = _sci_sp = None
try:
    import numpy as _np, scipy.integrate as _sci_int, scipy.sparse as _sci_sp
    if not hasattr(_sci_int, "simps") and hasattr(_sci_int, "simpson"):
        _sci_int.simps = _sci_int.simpson
    if not hasattr(_sci_int, "trapz") and hasattr(_sci_int, "trapezoid"):
        _sci_int.trapz = _sci_int.trapezoid
    if not hasattr(_sci_int, "cumtrapz") and hasattr(_sci_int, "cumulative_trapezoid"):
        _sci_int.cumtrapz = _sci_int.cumulative_trapezoid
except Exception:
    pass
if "scicode.compare.cmp" not in _sys.modules:
    def _allclose(a, b, atol=1e-8, rtol=1e-5):
        return bool(_np.allclose(a, b, atol=atol, rtol=rtol)) if _np is not None else a == b
    def _are_dicts_close(d1, d2, atol=1e-5, rtol=1e-2):
        if d1.keys() != d2.keys():
            return False
        for k in d1:
            if not _allclose(d1[k], d2[k], atol=atol, rtol=rtol):
                return False
        return True
    def _are_csc_matrix_close(m1, m2):
        return _allclose(m1.toarray(), m2.toarray())
    def _cmp_tuple_or_list(var1, var2):
        if len(var1) != len(var2):
            return False
        for v1, v2 in zip(var1, var2):
            if isinstance(v1, dict):
                if not _are_dicts_close(v1, v2):
                    return False
            elif _sci_sp is not None and isinstance(v1, _sci_sp.csc_matrix):
                if not _are_csc_matrix_close(v1, v2):
                    return False
            elif isinstance(v1, bool):
                if not (v1 == v2):
                    return False
            else:
                try:
                    if not _allclose(v1, v2):
                        return False
                except ValueError as e:
                    if "truth value of an array with more than one element is ambiguous" in str(e):
                        import sympy as _sympy
                        if len(v1) != len(v2):
                            return False
                        for s1, s2 in zip(v1, v2):
                            if _sympy.simplify(s1 - s2) != 0:
                                return False
                    else:
                        return False
        return True
    _m_scicode = _types.ModuleType("scicode")
    _m_compare = _types.ModuleType("scicode.compare")
    _m_cmp = _types.ModuleType("scicode.compare.cmp")
    _m_cmp.are_dicts_close = _are_dicts_close
    _m_cmp.are_csc_matrix_close = _are_csc_matrix_close
    _m_cmp.cmp_tuple_or_list = _cmp_tuple_or_list
    _sys.modules.setdefault("scicode", _m_scicode)
    _sys.modules.setdefault("scicode.compare", _m_compare)
    _sys.modules["scicode.compare.cmp"] = _m_cmp
"""


def _indent(s: str) -> str:
    return "\n".join("    " + ln for ln in s.splitlines()) or "    pass"


def _program(head: str, code: str, tail: str, body: str) -> str:
    return "\n\n".join([SHIM, head, code, tail, "import pickle, json, sys", body,
                        f"print('{MARK}' + json.dumps(_res))"])


def _run(text: str, tmp: Path, python: str, timeout: float) -> list | None:
    f = tmp / "agree_run.py"
    f.write_text(text)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp),
           "MPLCONFIGDIR": str(tmp), "OMP_NUM_THREADS": "1",
           "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    try:
        p = subprocess.run([python, str(f)], capture_output=True, text=True, timeout=timeout,
                           cwd=tmp, env=env)
    except subprocess.TimeoutExpired:
        return None
    for ln in reversed(p.stdout.splitlines()):
        if ln.startswith(MARK):
            return json.loads(ln[len(MARK):])
    return None


def _values(bundle: dict, code: str, tmp: Path, python: str, timeout: float) -> list:
    """One entry per case: the path of the pickled value, or None when no value was produced."""
    head, tail = bundle["head"], bundle["tail"]
    lines = ["_res = []"]
    for k, c in enumerate(bundle["cases"]):
        out = tmp / f"v{k}.pkl"
        lines += ["try:", _indent(c["setup"]), f"    _x = {c['expr']}",
                  f"    open({str(out)!r}, 'wb').write(pickle.dumps(_x))",
                  f"    _res.append({str(out)!r})", "except BaseException:", "    _res.append(None)"]
    res = _run(_program(head, code, tail, "\n".join(lines)), tmp, python, timeout)
    return res if res is not None else [None] * len(bundle["cases"])


def _compare(bundle: dict, code_a: str, vals_a: list, vals_b: list, tmp: Path, python: str,
             timeout: float) -> list:
    """Per case: True when the official assertion holds with A's value against B's value."""
    head, tail = bundle["head"], bundle["tail"]
    lines = ["_res = []"]
    for k, c in enumerate(bundle["cases"]):
        if vals_a[k] is None or vals_b[k] is None:
            lines.append("_res.append(False)")
            continue
        lines += ["try:", _indent(c["setup"]),
                  f"    _a = pickle.load(open({vals_a[k]!r}, 'rb'))",
                  f"    target = pickle.load(open({vals_b[k]!r}, 'rb'))",
                  f"    _res.append(bool({c['cmp']}))",
                  "except BaseException:", "    _res.append(False)"]
    res = _run(_program(head, code_a, tail, "\n".join(lines)), tmp, python, timeout)
    return res if res is not None else [False] * len(bundle["cases"])


def check(bundle: dict, code_a: str, code_b: str, python: str = sys.executable,
          timeout: float = TIMEOUT_S) -> dict:
    """Compare two solutions on every bundled case. The bundle's `head`/`tail` are the same
    dependency and reference code the grader uses (the solution sits between them)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ta, tb = tmp / "a", tmp / "b"
        ta.mkdir()
        tb.mkdir()
        with ThreadPoolExecutor(2) as ex:      # the two solutions are independent
            fa = ex.submit(_values, bundle, code_a, ta, python, timeout)
            fb = ex.submit(_values, bundle, code_b, tb, python, timeout)
            va, vb = fa.result(), fb.result()
        ok = _compare(bundle, code_a, va, vb, tmp, python, timeout)
    failed = []
    for k, c in enumerate(bundle["cases"]):
        if ok[k]:
            continue
        if va[k] is None or vb[k] is None:
            reason = "no_value_a" if va[k] is None else "no_value_b"
        else:
            reason = "mismatch"
        failed.append({"sid": c["sid"], "i": c["i"], "reason": reason})
    return {"agree": not failed, "checked": len(bundle["cases"]),
            "excluded": bundle.get("excluded", 0), "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)
    bundle = json.loads(a.bundle.read_text())
    code_a = a.a.read_text() if a.a.exists() else ""
    code_b = a.b.read_text() if a.b.exists() else ""
    # absolute, not resolved: a venv's python is a symlink and resolving it drops the venv
    rep = check(bundle, code_a, code_b, os.path.abspath(a.python), a.timeout)
    print(json.dumps(rep, sort_keys=True))
    return 0 if rep["agree"] else 1


if __name__ == "__main__":
    sys.exit(main())
