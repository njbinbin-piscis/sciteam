"""Comprehensive Scientific Benchmark Tools for Long-Horizon Research Agents (`sciteam/harness/tools/scientific.py`).

Extends `BUILTIN_TOOLS` with 6 scientific & engineering primitives:
  1. `glob` — Fast recursive file tree pattern matching inside the sandbox workspace.
  2. `grep` — Full-text regex search across code, data headers, and markdown notes with line numbers.
  3. `python_repl` — Stateful scientific Python kernel that preserves variables, matrices, models,
     and DataFrames across steps within the workspace (via `.sciteam_repl_state.pkl` + AST last-expr
     auto-display) and records generated plots/artifacts.
  4. `inspect_dataset` — Rapid schema, shape, missing-value, and quantile summary for `.csv`, `.tsv`,
     `.json`, `.jsonl`, and `.npy` datasets without flooding context.
  5. `todo_plan` — Structured scientific hypothesis & milestone tracker (`pending`, `in_progress`,
     `completed`, `refuted`) persisted to `.sciteam_plan.json` for Codex UI pinning.
  6. `log_experiment` — Structured experiment ledger (`params`, `metrics`, `invariants_passed`,
     `notes`, `artifact_path`) persisted to `.sciteam_experiments.jsonl`.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.types import ToolSpec


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


class GlobTool:
    readonly = True
    spec = ToolSpec(
        name="glob",
        description=(
            "Find files in the workspace matching a glob pattern (e.g. '*.py', '**/*.csv', "
            "'data/*.json'). Returns matching relative paths sorted by modification time."
        ),
        parameters=_obj(
            {
                "pattern": {"type": "string", "description": "Glob pattern to match files against."},
                "max_results": {"type": "integer", "description": "Maximum paths to return (default 100)."},
            },
            ["pattern"],
        ),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args["pattern"]).strip() or "*"
        max_results = max(1, int(args.get("max_results") or 100))
        root = Path(ctx.sandbox.workdir)
        if not root.exists():
            return ToolResult(content="[workspace directory does not exist]", is_error=True)

        matches: list[tuple[float, str]] = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if any(part in ("venv", ".venv", "__pycache__", ".git", "node_modules") or part.startswith(".sciteam_") for part in rel.split("/")):
                continue
            if (
                fnmatch.fnmatch(rel, pattern)
                or fnmatch.fnmatch(p.name, pattern)
                or (pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]))
            ):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matches.append((mtime, rel))

        matches.sort(key=lambda item: (-item[0], item[1]))
        selected = [rel for _, rel in matches[:max_results]]
        if not selected:
            return ToolResult(
                content=f"[no files matched pattern {pattern!r}]",
                meta={"count": 0, "pattern": pattern},
            )
        extra = (
            f"\n[... {len(matches) - len(selected)} more matches omitted ...]"
            if len(matches) > len(selected)
            else ""
        )
        return ToolResult(
            content="\n".join(selected) + extra,
            meta={"count": len(selected), "total_matches": len(matches), "pattern": pattern},
        )


class GrepTool:
    readonly = True
    spec = ToolSpec(
        name="grep",
        description=(
            "Search file contents in the workspace using regular expressions. Returns matching "
            "file paths, 1-based line numbers, and matching lines."
        ),
        parameters=_obj(
            {
                "pattern": {"type": "string", "description": "Regular expression pattern to search for."},
                "path_glob": {
                    "type": "string",
                    "description": "Optional file glob filter (e.g. '*.py', '*.md').",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search."},
                "max_matches": {"type": "integer", "description": "Maximum matches to return (default 60)."},
            },
            ["pattern"],
        ),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_pat = str(args["pattern"])
        path_glob = str(args.get("path_glob") or "*").strip()
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        max_matches = max(1, int(args.get("max_matches") or 60))
        try:
            rx = re.compile(raw_pat, flags)
        except re.error as exc:
            return ToolResult(content=f"error: invalid regex {raw_pat!r}: {exc}", is_error=True)

        root = Path(ctx.sandbox.workdir)
        hits: list[str] = []
        files_matched = 0
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if any(part in ("venv", ".venv", "__pycache__", ".git", "node_modules") or part.startswith(".sciteam_") for part in rel.split("/")) or rel.endswith(".pkl"):
                continue
            if path_glob != "*" and not (
                fnmatch.fnmatch(rel, path_glob) or fnmatch.fnmatch(p.name, path_glob)
            ):
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue
            text = raw.decode("utf-8", errors="replace")
            file_had_hit = False
            for idx, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    file_had_hit = True
                    snippet = line[:240] + ("…" if len(line) > 240 else "")
                    hits.append(f"{rel}:{idx}: {snippet}")
                    if len(hits) >= max_matches:
                        break
            if file_had_hit:
                files_matched += 1
            if len(hits) >= max_matches:
                break

        if not hits:
            return ToolResult(
                content=f"[no matches found for {raw_pat!r}]",
                meta={"matches": 0, "files_matched": 0},
            )
        return ToolResult(
            content="\n".join(hits),
            meta={"matches": len(hits), "files_matched": files_matched, "pattern": raw_pat},
        )


_REPL_DRIVER_CODE = r"""
import ast, json, math, os, pickle, sys, traceback
from pathlib import Path

state_file = Path(".sciteam_repl_state.pkl")
code_file = Path(".sciteam_repl_cell.py")
reset_flag = os.environ.get("SCITEAM_REPL_RESET") == "1"

ns = {"__name__": "__main__", "math": math, "json": json, "Path": Path}
if state_file.exists() and not reset_flag:
    try:
        loaded = pickle.loads(state_file.read_bytes())
        if isinstance(loaded, dict):
            ns.update(loaded)
    except Exception:
        pass

source = code_file.read_text(encoding="utf-8")
last_val = None
exit_ok = True
try:
    tree = ast.parse(source, filename="<python_repl>", mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)
        if tree.body:
            exec(compile(tree, "<python_repl>", "exec"), ns)
        last_val = eval(compile(last_expr, "<python_repl>", "eval"), ns)
        if last_val is not None:
            print(repr(last_val))
    else:
        exec(compile(tree, "<python_repl>", "exec"), ns)
except Exception:
    exit_ok = False
    traceback.print_exc()

persistable = {}
summary_vars = {}
for k, v in list(ns.items()):
    if k.startswith("_") or k in ("math", "json", "Path", "ast", "pickle", "sys", "os", "traceback"):
        continue
    if callable(v) or isinstance(v, type):
        continue
    try:
        pickle.dumps(v)
        persistable[k] = v
        r = repr(v)
        summary_vars[k] = f"{type(v).__name__}: {r[:80]}"
    except Exception:
        summary_vars[k] = f"{type(v).__name__} (non-picklable)"

try:
    state_file.write_bytes(pickle.dumps(persistable))
except Exception:
    pass

Path(".sciteam_repl_vars.json").write_text(
    json.dumps({"ok": exit_ok, "variables": summary_vars}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
sys.exit(0 if exit_ok else 1)
"""


class PythonReplTool:
    readonly = False
    spec = ToolSpec(
        name="python_repl",
        description=(
            "Execute Python code in a stateful scientific computing kernel. Variables, arrays, "
            "DataFrames, and computed results persist across calls within the same workspace. "
            "If the last statement is an expression, its repr is automatically printed. "
            "Set reset=true to clear the kernel memory."
        ),
        parameters=_obj(
            {
                "code": {"type": "string", "description": "Python code cell to execute."},
                "reset": {"type": "boolean", "description": "Clear persisted variables before running."},
                "timeout_s": {"type": "number", "description": "Execution timeout in seconds (default 60)."},
            },
            ["code"],
        ),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = str(args["code"])
        reset = bool(args.get("reset", False))
        timeout = float(args.get("timeout_s") or 60.0)
        timeout = max(1.0, min(timeout, ctx.config.sandbox.cmd_max_timeout_s, ctx.remaining_wall_s))

        await ctx.sandbox.write_file(".sciteam_repl_cell.py", code.encode("utf-8"))
        await ctx.sandbox.write_file(".sciteam_repl_driver.py", _REPL_DRIVER_CODE.encode("utf-8"))
        env_prefix = "SCITEAM_REPL_RESET=1 " if reset else ""
        res = await ctx.sandbox.run(f"{env_prefix}python3 .sciteam_repl_driver.py", timeout_s=timeout)

        vars_meta: dict[str, str] = {}
        try:
            raw_meta = await ctx.sandbox.read_file(".sciteam_repl_vars.json")
            parsed_meta = json.loads(raw_meta.decode("utf-8", errors="replace"))
            vars_meta = parsed_meta.get("variables") or {}
        except Exception:
            pass

        output_lines: list[str] = []
        if res.output.strip():
            output_lines.append(res.output.rstrip("\n"))
        else:
            output_lines.append("[cell executed with no stdout]")

        if vars_meta:
            shown_vars = ", ".join(f"{k} ({v.split(':')[0]})" for k, v in list(vars_meta.items())[:12])
            output_lines.append(f"[persisted kernel variables ({len(vars_meta)}): {shown_vars}]")

        is_err = bool(res.timed_out or (res.exit_code is not None and res.exit_code != 0))
        return ToolResult(
            content="\n".join(output_lines),
            is_error=is_err,
            meta={
                "exit_code": res.exit_code,
                "duration_s": round(res.duration_s, 3),
                "variables": vars_meta,
                "reset": reset,
            },
        )


class InspectDatasetTool:
    readonly = True
    spec = ToolSpec(
        name="inspect_dataset",
        description=(
            "Inspect a scientific dataset file (.csv, .tsv, .json, .jsonl) in the workspace. "
            "Returns row/column dimensions, column names, inferred data types, missing value "
            "counts, numeric min/mean/max summaries, and sample rows."
        ),
        parameters=_obj(
            {
                "path": {"type": "string", "description": "Relative path to the dataset file."},
                "sample_rows": {"type": "integer", "description": "Number of sample rows to preview (default 5)."},
            },
            ["path"],
        ),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rel_path = str(args["path"]).strip()
        sample_n = max(1, min(20, int(args.get("sample_rows") or 5)))
        try:
            raw = await ctx.sandbox.read_file(rel_path)
        except Exception as exc:
            return ToolResult(content=f"error: cannot read dataset {rel_path!r}: {exc}", is_error=True)

        text = raw.decode("utf-8", errors="replace")
        rows: list[dict[str, Any]] = []
        columns: list[str] = []

        if rel_path.endswith((".csv", ".tsv")):
            delim = "\t" if rel_path.endswith(".tsv") else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            columns = list(reader.fieldnames or [])
            for r in reader:
                rows.append(dict(r))
        elif rel_path.endswith(".jsonl"):
            for line in text.splitlines():
                if line.strip():
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
            if rows:
                columns = list(rows[0].keys())
        elif rel_path.endswith(".json"):
            try:
                parsed = json.loads(text)
            except Exception as exc:
                return ToolResult(content=f"error: invalid JSON in {rel_path!r}: {exc}", is_error=True)
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                rows = parsed
                columns = list(parsed[0].keys())
            elif isinstance(parsed, dict):
                summary = {
                    "path": rel_path,
                    "format": "json_object",
                    "top_level_keys": list(parsed.keys()),
                    "preview": {k: str(v)[:120] for k, v in list(parsed.items())[:15]},
                }
                return ToolResult(
                    content=json.dumps(summary, indent=2, ensure_ascii=False),
                    meta={"format": "json_object", "keys": len(parsed)},
                )

        col_stats: dict[str, dict[str, Any]] = {}
        for col in columns[:30]:
            vals = [r.get(col) for r in rows]
            missing = sum(1 for v in vals if v is None or str(v).strip() == "")
            nums: list[float] = []
            for v in vals:
                if v is None or str(v).strip() == "":
                    continue
                try:
                    nums.append(float(v))
                except (ValueError, TypeError):
                    pass
            if nums and len(nums) >= max(1, (len(vals) - missing) // 2):
                col_stats[col] = {
                    "type": "numeric",
                    "missing": missing,
                    "min": round(min(nums), 6),
                    "mean": round(sum(nums) / len(nums), 6),
                    "max": round(max(nums), 6),
                }
            else:
                distinct = len({str(v) for v in vals if v is not None and str(v).strip() != ""})
                col_stats[col] = {
                    "type": "categorical/text",
                    "missing": missing,
                    "distinct_count": distinct,
                }

        report = {
            "path": rel_path,
            "row_count": len(rows),
            "column_count": len(columns),
            "columns": col_stats,
            "sample_rows": rows[:sample_n],
        }
        return ToolResult(
            content=json.dumps(report, indent=2, ensure_ascii=False),
            meta={"row_count": len(rows), "column_count": len(columns)},
        )


class TodoPlanTool:
    readonly = False
    spec = ToolSpec(
        name="todo_plan",
        description=(
            "Maintain the structured research plan, scientific hypotheses, and milestones for "
            "long-horizon tasks. Persists to `.sciteam_plan.json` and updates the live UI plan bar."
        ),
        parameters=_obj(
            {
                "items": {
                    "type": "array",
                    "description": "List of research plan items/hypotheses.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "refuted"],
                            },
                            "hypothesis": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                        "required": ["id", "title", "status"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "If true, merge items by id into the existing plan (default true).",
                },
            },
            ["items"],
        ),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        merge = bool(args.get("merge", True))
        existing: list[dict[str, Any]] = []
        if merge:
            try:
                raw = await ctx.sandbox.read_file(".sciteam_plan.json")
                data = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    existing = data["items"]
            except Exception:
                existing = []

        by_id: dict[str, dict[str, Any]] = {str(it.get("id")): dict(it) for it in existing if it.get("id")}
        if not merge:
            by_id.clear()

        for raw_item in args.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            iid = str(raw_item.get("id") or new_id("step"))
            prev = by_id.get(iid, {})
            prev.update(
                {
                    "id": iid,
                    "title": str(raw_item.get("title") or prev.get("title") or iid),
                    "status": str(raw_item.get("status") or prev.get("status") or "pending"),
                    "hypothesis": str(raw_item.get("hypothesis") or prev.get("hypothesis") or ""),
                    "evidence": str(raw_item.get("evidence") or prev.get("evidence") or ""),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            by_id[iid] = prev

        items_list = list(by_id.values())
        payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "items": items_list}
        await ctx.sandbox.write_file(
            ".sciteam_plan.json",
            (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        lines = [f"[Research Plan Updated — {len(items_list)} item(s)]"]
        for it in items_list:
            badge = {
                "completed": "[x]",
                "in_progress": "[~]",
                "refuted": "[!]",
                "pending": "[ ]",
            }.get(it["status"], "[ ]")
            ev = f" — evidence: {it['evidence']}" if it.get("evidence") else ""
            lines.append(f"  {badge} ({it['id']}) {it['title']} [{it['status']}]{ev}")
        return ToolResult(
            content="\n".join(lines),
            meta={"item_count": len(items_list), "items": items_list},
        )


class LogExperimentTool:
    readonly = False
    spec = ToolSpec(
        name="log_experiment",
        description=(
            "Record a structured scientific experiment run (parameters, quantitative metrics, "
            "domain invariant pass/fail status, and output artifact path) to `.sciteam_experiments.jsonl`."
        ),
        parameters=_obj(
            {
                "experiment_name": {"type": "string", "description": "Short name or hypothesis ID."},
                "parameters": {"type": "object", "description": "Input model parameters or hyper-parameters."},
                "metrics": {"type": "object", "description": "Quantitative output metrics (e.g. rmse, r2, energy)."},
                "invariants_passed": {
                    "type": "boolean",
                    "description": "Whether all physical/mathematical domain invariants passed.",
                },
                "artifact_path": {"type": "string", "description": "Optional path to output script/report/plot."},
                "notes": {"type": "string", "description": "Brief scientific observation or conclusion."},
            },
            ["experiment_name", "metrics", "invariants_passed"],
        ),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        entry = {
            "exp_id": new_id("exp"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": ctx.state.steps + 1,
            "experiment_name": str(args["experiment_name"]),
            "parameters": dict(args.get("parameters") or {}),
            "metrics": dict(args.get("metrics") or {}),
            "invariants_passed": bool(args["invariants_passed"]),
            "artifact_path": str(args.get("artifact_path") or ""),
            "notes": str(args.get("notes") or ""),
        }
        existing_lines: list[str] = []
        try:
            raw = await ctx.sandbox.read_file(".sciteam_experiments.jsonl")
            existing_lines = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        except Exception:
            existing_lines = []

        existing_lines.append(json.dumps(entry, ensure_ascii=False))
        await ctx.sandbox.write_file(
            ".sciteam_experiments.jsonl",
            ("\n".join(existing_lines) + "\n").encode("utf-8"),
        )
        return ToolResult(
            content=(
                f"[Experiment Logged: {entry['exp_id']} ({entry['experiment_name']}) | "
                f"invariants_passed={entry['invariants_passed']} | metrics={json.dumps(entry['metrics'], ensure_ascii=False)}]"
            ),
            meta=entry,
        )


SCIENTIFIC_BUILTIN_TOOLS = {
    t.spec.name: t
    for t in (
        GlobTool(),
        GrepTool(),
        PythonReplTool(),
        InspectDatasetTool(),
        TodoPlanTool(),
        LogExperimentTool(),
    )
}
