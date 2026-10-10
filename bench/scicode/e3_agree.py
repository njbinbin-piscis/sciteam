"""E3 (docs/60 §6.6): do two independent wrong solutions produce the same wrong output?

For every solution (one workspace per run) and every test case, record (a) whether the case
passes against the real target and (b) the value compared with `target`, pickled. Then, for
each pair of solutions of the same problem and each case where at least one of them fails,
check agreement with the official comparator, using solution B's value as the target for A's.

Usage: e3_agree.py --sol name=experiment_dir ... --out DIR
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import pickle
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import grade  # noqa: E402

PY = dict(grade.ENVS)["2025"]
TIMEOUT = 600
MARK = "@@E3@@"


def split_case(tc: str) -> tuple[str, str] | None:
    """(setup, expression compared with `target`) or None when the form is not recognised."""
    try:
        tree = ast.parse(tc)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[-1], ast.Assert):
        return None
    test = tree.body[-1].test
    expr = None
    for node in ast.walk(test):
        if isinstance(node, ast.Call):
            args = node.args
            if len(args) >= 2 and any(isinstance(a, ast.Name) and a.id == "target" for a in args):
                others = [a for a in args if not (isinstance(a, ast.Name) and a.id == "target")]
                expr = others[0]
                break
        if isinstance(node, ast.Compare) and len(node.comparators) == 1:
            sides = [node.left, node.comparators[0]]
            if any(isinstance(s, ast.Name) and s.id == "target" for s in sides):
                expr = next(s for s in sides if not (isinstance(s, ast.Name) and s.id == "target"))
                break
    if expr is None:
        return None
    setup = "\n".join(ast.unparse(s) for s in tree.body[:-1])
    return setup, ast.unparse(expr)


def code_for(pid: str, workspace: Path) -> str:
    prob = grade.problem(pid)
    sol = workspace / grade.SOLUTION
    agent = sol.read_text() if sol.exists() else ""
    given = [grade.gold(s["step_number"]) for k, s in enumerate(prob["sub_steps"]) if (pid, k) in grade.SKIP]
    return "\n\n".join([prob["required_dependencies"], agent, *given])


def indent(s: str) -> str:
    return "\n".join("    " + ln for ln in s.splitlines()) or "    pass"


def run(text: str, tmp: Path) -> dict | None:
    f = tmp / "e3.py"
    f.write_text(text)
    try:
        p = subprocess.run(
            [str(PY), str(f)], capture_output=True, text=True, timeout=TIMEOUT, cwd=tmp,
            env={"PATH": "/usr/bin:/bin", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
        )
    except subprocess.TimeoutExpired:
        return None
    for ln in reversed(p.stdout.splitlines()):
        if ln.startswith(MARK):
            return json.loads(ln[len(MARK):])
    return None


def capture(pid: str, ws: Path, outdir: Path) -> dict:
    """{step: [{"ok": bool, "val": path|None} per case]} for one solution."""
    prob = grade.problem(pid)
    code = code_for(pid, ws)
    res = {}
    with tempfile.TemporaryDirectory() as td:
        for k, s in enumerate(prob["sub_steps"]):
            if (pid, k) in grade.SKIP or not s["test_cases"]:
                continue
            sid, cases = s["step_number"], s["test_cases"]
            lines = [code, "", "import sys, json as _j, pickle as _p",
                     f"sys.path.insert(0, r'{grade.VENDOR.as_posix()}')",
                     "from scicode.parse.parse import process_hdf5_to_tuple",
                     f"targets = process_hdf5_to_tuple('{sid}', {len(cases)}, r'{grade.H5.as_posix()}')",
                     "_r = []"]
            for i, tc in enumerate(cases):
                sp = split_case(tc)
                vf = outdir / f"{sid}.{i}.pkl"
                lines += ["_ok = False", "_v = None", "try:", f"    target = targets[{i}]", indent(tc),
                          "    _ok = True", "except BaseException:", "    pass"]
                if sp:
                    lines += ["try:", indent(sp[0]), f"    _x = {sp[1]}",
                              f"    open(r'{vf}', 'wb').write(_p.dumps(_x))", f"    _v = r'{vf}'",
                              "except BaseException:", "    pass"]
                lines.append("_r.append({'ok': _ok, 'val': _v})")
            lines.append(f"print('{MARK}' + _j.dumps(_r))")
            out = run("\n".join(lines), Path(td))
            res[sid] = out if out is not None else [{"ok": False, "val": None}] * len(cases)
    return res


def agree(pid: str, ws_a: Path, sid: str, i: int, va: str, vb: str) -> bool | None:
    """Official comparator with A's value in place of the expression and B's value as target."""
    prob = grade.problem(pid)
    tc = next(s for s in prob["sub_steps"] if s["step_number"] == sid)["test_cases"][i]
    sp = split_case(tc)
    tree = ast.parse(tc)
    test = ast.unparse(tree.body[-1].test).replace(sp[1], "_a", 1)
    text = "\n".join([code_for(pid, ws_a), "", "import sys, json as _j, pickle as _p",
                      f"sys.path.insert(0, r'{grade.VENDOR.as_posix()}')",
                      f"_a = _p.loads(open(r'{va}', 'rb').read())",
                      f"target = _p.loads(open(r'{vb}', 'rb').read())",
                      "_ok = None", "try:", indent(sp[0]), f"    _ok = bool({test})",
                      "except BaseException:", "    _ok = False",
                      f"print('{MARK}' + _j.dumps(_ok))"])
    with tempfile.TemporaryDirectory() as td:
        return run(text, Path(td))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sol", action="append", required=True, help="name=experiment_dir")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()
    a.out = a.out.resolve()
    a.out.mkdir(parents=True, exist_ok=True)
    sols = {}  # (model, pid, r) -> workspace
    for spec in a.sol:
        name, d = spec.split("=", 1)
        for ws in sorted(Path(d).glob("single/p*/r*/workspace")):
            pid, r = ws.parent.parent.name[1:], int(ws.parent.name[1:])
            sols[(name, pid, r)] = ws

    cap_file = a.out / "capture.json"
    caps = json.loads(cap_file.read_text()) if cap_file.exists() else {}

    def do_cap(key):
        k = "|".join(map(str, key))
        if k in caps:
            return k, caps[k]
        od = a.out / "vals" / k.replace("|", "_")
        od.mkdir(parents=True, exist_ok=True)
        return k, capture(key[1], sols[key], od)

    with ThreadPoolExecutor(a.jobs) as ex:
        for n, (k, v) in enumerate(ex.map(do_cap, sols), 1):
            caps[k] = v
            if n % 16 == 0:
                cap_file.write_text(json.dumps(caps))
    cap_file.write_text(json.dumps(caps))

    byp = defaultdict(list)
    for key in sols:
        byp[key[1]].append(key)
    jobs = []
    for pid, keys in byp.items():
        for ka, kb in itertools.combinations(sorted(keys), 2):
            ca, cb = caps["|".join(map(str, ka))], caps["|".join(map(str, kb))]
            for sid in ca:
                for i, (x, y) in enumerate(zip(ca[sid], cb.get(sid, []))):
                    if x["ok"] and y["ok"]:
                        continue
                    jobs.append((ka, kb, sid, i, x, y))

    def do_agree(j):
        ka, kb, sid, i, x, y = j
        if not (x["val"] and y["val"]):
            ag = None
        else:
            ag = agree(ka[1], sols[ka], sid, i, x["val"], y["val"])
        return {"a": list(ka), "b": list(kb), "step": sid, "case": i, "ok_a": x["ok"], "ok_b": y["ok"],
                "agree": ag}

    with ThreadPoolExecutor(a.jobs) as ex:
        rows = list(ex.map(do_agree, jobs))
    (a.out / "pairs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    stat = defaultdict(Counter)
    for r in rows:
        kind = "same" if r["a"][0] == r["b"][0] else "cross"
        grp = f"{r['a'][0]}~{r['b'][0]}" if kind == "cross" else f"{r['a'][0]}~{r['a'][0]}"
        if kind == "same" and r["a"][2] == r["b"][2]:
            continue
        both = "both_wrong" if not (r["ok_a"] or r["ok_b"]) else "one_wrong"
        tag = {True: "agree", False: "disagree", None: "unknown"}[r["agree"]]
        stat[grp][f"{both}:{tag}"] += 1
    lines = ["# E3: output agreement on failing cases (official comparator, B's value as target)", "",
             "| pair | both wrong: agree | both wrong: disagree | one wrong: agree | one wrong: disagree "
             "| unknown | P(disagree given both wrong) |", "|---|---|---|---|---|---|---|"]
    res = {}
    for g, c in sorted(stat.items()):
        bw = c["both_wrong:agree"] + c["both_wrong:disagree"]
        p = round(c["both_wrong:disagree"] / bw, 3) if bw else None
        res[g] = {**c, "p_disagree_given_both_wrong": p}
        unk = c["both_wrong:unknown"] + c["one_wrong:unknown"]
        lines.append(f"| {g} | {c['both_wrong:agree']} | {c['both_wrong:disagree']} | "
                     f"{c['one_wrong:agree']} | {c['one_wrong:disagree']} | {unk} | {p} |")
    (a.out / "analysis.json").write_text(json.dumps(res, indent=2) + "\n")
    md = "\n".join(lines) + "\n"
    (a.out / "analysis.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
