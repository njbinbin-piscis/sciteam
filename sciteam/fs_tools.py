"""Workspace-confined filesystem tools + controlled bash for the own-worker
arm (B4: harness-SOTA parity with the pi arm's always-on native
``read``/``write``/``edit``/``grep``/``find``/``ls`` and capability-gated
``bash`` — see ``sciteam/pi_runtime.py`` and ``HARNESS_SOTA_AUDIT.md``).

Before this module, `LlmWorkerRuntime` could only write files in one shot at
round end via ``envelope["files"]`` (flattened into the mission dir, no
subdirectories, no read-back) — it could not inspect an existing file,
search the workspace, or incrementally patch anything mid-round. That gap is
exactly what made the `build_fix_loop` seat web-search for a local problem
spec it could not `read` (see HARNESS_SOTA_AUDIT.md §build_fix_loop). These
tools close it without a fork: same workspace-confinement discipline as
`sciteam.sandbox.LocalPythonSandbox` (resolve, `relative_to` check, never
escape), same tool-call loop `ToolRegistry` both arms' research tools go
through.

Every tool takes ``(arguments, context)`` where ``context["mission_workspace"]``
is the same directory `compute_sandbox`/`model_certify` already scope to
(the mission's shared artifact/pack dir — see `llm_worker.py`'s
`tool_context` construction), so file ops, the Python sandbox and `bash` all
see one consistent tree.

``bash`` is deliberately *not* network-isolated the way the Python model
sandbox is (that isolation exists so declared, hash-locked models stay
reproducible — a different property than what a research/build shell needs).
It is instead: cwd-confined to the mission workspace, wall-clock + CPU +
memory bounded, output-truncated, and has secret-suffixed env vars
(``*_API_KEY``/``*_TOKEN``/``*_SECRET``/``*_PASSWORD``) stripped so a
prompt-injected command cannot exfiltrate the LLM/HPC credentials of the
process that spawned it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import sciteam.limits as limits
from sciteam.output_truncation import persist_full_text, truncate_head, truncate_tail

# Compat aliases — actual caps come from ``sciteam.limits`` (env-overridable).
MAX_READ_CHARS = limits.OUTPUT_MAX_BYTES
MAX_GREP_MATCHES = 200
MAX_FIND_RESULTS = 200
MAX_BASH_OUTPUT_CHARS = limits.OUTPUT_MAX_BYTES
DEFAULT_BASH_TIMEOUT_S = 30
MAX_BASH_TIMEOUT_S = 120

# Env vars whose *names* end with one of these are dropped from the bash
# child's environment — defense in depth against a prompt-injected `env` /
# `printenv` / `curl ... -d "$SOME_API_KEY"` exfiltration attempt.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


class WorkspaceError(PermissionError):
    """A tool argument tried to name a path outside the mission workspace."""


def _workspace_from_context(context: dict[str, Any]) -> Path:
    raw = context.get("mission_workspace")
    if not raw:
        raise WorkspaceError("mission workspace was not supplied")
    workspace = Path(str(raw)).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _resolve(workspace: Path, raw: str) -> Path:
    candidate = (workspace / str(raw or ".")).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise WorkspaceError(f"path escapes mission workspace: {raw}") from exc
    return candidate


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def read_file(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
        raw = str(args.get("path") or "")
        path = _resolve(workspace, raw)
    except WorkspaceError as exc:
        return _err(str(exc))
    if not path.is_file():
        return _err(f"not a file: {raw}")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(1, int(args.get("offset") or 1))
    limit = args.get("limit")
    selected = lines[offset - 1 : offset - 1 + max(1, int(limit))] if limit else lines[offset - 1 :]
    body = "\n".join(selected)
    cut = truncate_head(
        body, max_lines=limits.output_max_lines(), max_bytes=limits.output_max_bytes()
    )
    next_offset = offset + cut.output_lines if cut.truncated else None
    return {
        "ok": True,
        "path": str(path.relative_to(workspace)),
        "total_lines": len(lines),
        "offset": offset,
        "text": cut.content,
        "truncated": cut.truncated,
        "truncated_by": cut.truncated_by,
        "total_bytes": cut.total_bytes,
        "output_lines": cut.output_lines,
        "output_bytes": cut.output_bytes,
        "next_offset": next_offset,
    }


def write_file(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
        raw = str(args.get("path") or "")
        if not raw:
            return _err("path is required")
        path = _resolve(workspace, raw)
    except WorkspaceError as exc:
        return _err(str(exc))
    content = args.get("content")
    if not isinstance(content, str):
        return _err("content must be a string")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "path": str(path.relative_to(workspace)),
        "bytes": len(content.encode("utf-8")),
    }


def edit_file(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
        raw = str(args.get("path") or "")
        path = _resolve(workspace, raw)
    except WorkspaceError as exc:
        return _err(str(exc))
    if not path.is_file():
        return _err(f"not a file: {raw}")
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(old, str) or not old:
        return _err("old_string is required and must be a non-empty string")
    if not isinstance(new, str):
        return _err("new_string must be a string")
    replace_all = bool(args.get("replace_all"))
    text = path.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return _err("old_string not found in file", path=raw)
    if count > 1 and not replace_all:
        return _err(
            f"old_string is not unique ({count} occurrences); add more context "
            "or pass replace_all=true",
            path=raw,
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    return {
        "ok": True,
        "path": str(path.relative_to(workspace)),
        "occurrences_replaced": count if replace_all else 1,
    }


def list_dir(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
        raw = str(args.get("path") or ".")
        path = _resolve(workspace, raw)
    except WorkspaceError as exc:
        return _err(str(exc))
    if not path.is_dir():
        return _err(f"not a directory: {raw}")
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: p.name):
        try:
            size = child.stat().st_size if child.is_file() else None
        except OSError:
            size = None
        entries.append({"name": child.name, "is_dir": child.is_dir(), "size": size})
    return {"ok": True, "path": str(path.relative_to(workspace)), "entries": entries}


def find_files(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
        root = _resolve(workspace, str(args.get("path") or "."))
    except WorkspaceError as exc:
        return _err(str(exc))
    if not root.is_dir():
        return _err(f"not a directory: {args.get('path')}")
    pattern = str(args.get("pattern") or "*")
    matches: list[str] = []
    truncated = False
    for candidate in sorted(root.rglob("*")):
        if fnmatch.fnmatch(candidate.name, pattern):
            matches.append(str(candidate.relative_to(workspace)))
            if len(matches) >= MAX_FIND_RESULTS:
                truncated = True
                break
    return {
        "ok": True,
        "pattern": pattern,
        "n": len(matches),
        "matches": matches,
        "truncated": truncated,
    }


def grep_files(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
        root = _resolve(workspace, str(args.get("path") or "."))
    except WorkspaceError as exc:
        return _err(str(exc))
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return _err("pattern is required")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _err(f"invalid regex: {exc}")
    glob = str(args.get("glob") or "*")
    candidates = [root] if root.is_file() else sorted(root.rglob(glob))
    matches: list[dict[str, Any]] = []
    truncated = False
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {
                        "path": str(candidate.relative_to(workspace)),
                        "line": lineno,
                        "text": line[:400],
                    }
                )
                if len(matches) >= MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    return {"ok": True, "n": len(matches), "matches": matches, "truncated": truncated}


def _bash_env(workspace: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(key.upper().endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES)
    }
    env["HOME"] = str(workspace)
    env["SCITEAM_BASH_TOOL"] = "1"
    return env


def _bash_limits(memory_mb: int, timeout_seconds: int):
    def apply() -> None:
        import resource

        mem = max(64, int(memory_mb)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        cpu = max(1, int(timeout_seconds)) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))

    return apply


def _archive_bash_stream(workspace: Path, stream: str, text: str) -> Path:
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    name = f"bash_{stream}_{int(time.time() * 1000)}_{digest}.txt"
    return persist_full_text(workspace / "tool_history", name, text)


def _bash_payload(
    workspace: Path,
    *,
    stdout: str,
    stderr: str,
    ok: bool,
    timed_out: bool = False,
    error: str | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    max_lines = limits.output_max_lines()
    max_bytes = limits.output_max_bytes()
    out_cut = truncate_tail(stdout, max_lines=max_lines, max_bytes=max_bytes)
    err_cut = truncate_tail(stderr, max_lines=max_lines, max_bytes=max_bytes)
    stdout_path = _archive_bash_stream(workspace, "stdout", stdout)
    stderr_path = _archive_bash_stream(workspace, "stderr", stderr)
    payload: dict[str, Any] = {
        "ok": ok,
        "stdout": out_cut.content,
        "stderr": err_cut.content,
        "truncated": out_cut.truncated or err_cut.truncated,
        "stdout_truncated": out_cut.truncated,
        "stderr_truncated": err_cut.truncated,
        "truncated_by": out_cut.truncated_by or err_cut.truncated_by,
        "total_lines": out_cut.total_lines,
        "total_bytes": out_cut.total_bytes,
        "full_output_path": str(stdout_path.relative_to(workspace)),
        "full_stderr_path": str(stderr_path.relative_to(workspace)),
    }
    if timed_out:
        payload["timed_out"] = True
    if error is not None:
        payload["error"] = error
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if out_cut.truncated:
        payload["stdout"] = (
            f"[truncated tail {out_cut.truncated_by}: "
            f"{out_cut.total_lines} lines / {out_cut.total_bytes} bytes; "
            f"full output: {payload['full_output_path']}]\n" + out_cut.content
        )
    if err_cut.truncated:
        payload["stderr"] = (
            f"[truncated tail {err_cut.truncated_by}: "
            f"{err_cut.total_lines} lines / {err_cut.total_bytes} bytes; "
            f"full output: {payload['full_stderr_path']}]\n" + err_cut.content
        )
    return payload


def run_bash(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        workspace = _workspace_from_context(context)
    except WorkspaceError as exc:
        return _err(str(exc))
    command = str(args.get("command") or "").strip()
    if not command:
        return _err("command is required")
    timeout = min(
        MAX_BASH_TIMEOUT_S, max(1, int(args.get("timeout_seconds") or DEFAULT_BASH_TIMEOUT_S))
    )
    try:
        completed = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=workspace,
            env=_bash_env(workspace),
            capture_output=True,
            timeout=timeout,
            preexec_fn=_bash_limits(1024, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _bash_payload(
            workspace,
            stdout=(exc.stdout or b"").decode(errors="replace"),
            stderr=(exc.stderr or b"").decode(errors="replace"),
            ok=False,
            timed_out=True,
            error=f"command timed out after {timeout}s",
        )
    except OSError as exc:
        return _err(f"failed to run bash: {exc}")
    return _bash_payload(
        workspace,
        stdout=completed.stdout.decode(errors="replace"),
        stderr=completed.stderr.decode(errors="replace"),
        ok=completed.returncode == 0,
        exit_code=completed.returncode,
    )


FS_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Read a text file from the mission workspace. Use before editing or "
                "citing an existing file's exact content — never guess file contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace"},
                    "offset": {
                        "type": "integer",
                        "description": "1-based start line",
                        "default": 1,
                    },
                    "limit": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Write (create or overwrite) a text file in the mission workspace. "
                "Subdirectories are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": (
                "Replace an exact substring in an existing workspace file. Fails if "
                "old_string is absent, or ambiguous (multiple matches) unless "
                "replace_all=true — same discipline as the editor's own edit tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List a mission-workspace directory's entries.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find",
            "description": "Find files under the mission workspace by filename glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "default": "*"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search mission-workspace files for a regex pattern; returns matching "
                "path/line/text triples."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "default": "*"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command cwd-confined to the mission workspace, with "
                "bounded time/CPU/memory and truncated output. Only available when "
                "your allowlisted skills already justify process-level access "
                "(compute/build or literature/web capabilities)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": DEFAULT_BASH_TIMEOUT_S},
                },
                "required": ["command"],
            },
        },
    },
]
