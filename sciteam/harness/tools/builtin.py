import re
from typing import Any

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.providers.llm.types import ToolSpec


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


class BashTool:
    readonly = False
    spec = ToolSpec(
        name="bash",
        description=(
            "Run a command in a persistent bash shell (cwd, environment variables and activated "
            "environments persist between calls). stdout and stderr are merged. stdin is "
            "/dev/null, so interactive programs will not wait for input. Long outputs are "
            "truncated in the middle; use read_output to page through the full text. "
            "Set timeout_s for long-running commands (e.g. full test suites)."),
        parameters=_obj({"command": {"type": "string"},
                         "timeout_s": {"type": "number",
                                       "description": "seconds before the command is killed"}},
                        ["command"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sc = ctx.config.sandbox
        timeout = float(args.get("timeout_s") or sc.cmd_default_timeout_s)
        timeout = max(1.0, min(timeout, sc.cmd_max_timeout_s, ctx.remaining_wall_s))
        r = await ctx.sandbox.run(args["command"], timeout_s=timeout)
        parts = [r.output.rstrip("\n")] if r.output else []
        if r.timed_out:
            parts.append(f"[command timed out after {timeout:.0f}s and was killed; the shell was "
                         f"restarted in the same directory, environment variables set by earlier "
                         f"commands were lost]")
        elif r.shell_restarted:
            parts.append("[the shell exited and was restarted in the same directory; environment "
                         "variables set by earlier commands were lost]")
        else:
            parts.append(f"[exit code: {r.exit_code}]")
        return ToolResult(content="\n".join(parts), is_error=bool(r.timed_out),
                          meta={"exit_code": r.exit_code, "timed_out": r.timed_out,
                                "duration_s": round(r.duration_s, 3)})


class ReadFileTool:
    readonly = True
    spec = ToolSpec(
        name="read_file",
        description=("Read a text file with line numbers. offset is the 1-based first line; "
                     "limit is the number of lines (default 2000). Very long lines are cut."),
        parameters=_obj({"path": {"type": "string"}, "offset": {"type": "integer"},
                         "limit": {"type": "integer"}}, ["path"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        to = ctx.config.tool_output
        try:
            data = await ctx.sandbox.read_file(args["path"])
        except (FileNotFoundError, OSError) as e:
            return ToolResult(content=f"error: cannot read {args['path']}: {e}", is_error=True)
        if b"\x00" in data[:8192]:
            return ToolResult(content=f"error: {args['path']} looks binary ({len(data)} bytes)",
                              is_error=True)
        lines = data.decode("utf-8", "replace").splitlines()
        offset = max(int(args.get("offset") or 1), 1)
        limit = max(int(args.get("limit") or to.read_file_default_lines), 1)
        chunk = lines[offset - 1: offset - 1 + limit]
        out = []
        for i, line in enumerate(chunk, start=offset):
            if len(line) > to.read_file_max_line_chars:
                line = line[: to.read_file_max_line_chars] + " …[line truncated]"
            out.append(f"{i:6}\t{line}")
        if not lines:
            return ToolResult(content="[empty file]")
        if offset > len(lines):
            return ToolResult(content=f"[offset {offset} beyond end of file ({len(lines)} lines)]",
                              is_error=True)
        end = offset - 1 + len(chunk)
        if end < len(lines):
            out.append(f"[showing lines {offset}-{end} of {len(lines)}; use offset={end + 1} "
                       f"to continue]")
        return ToolResult(content="\n".join(out))


class WriteFileTool:
    readonly = False
    spec = ToolSpec(
        name="write_file",
        description="Create or overwrite a file with the given content (parent dirs are created).",
        parameters=_obj({"path": {"type": "string"}, "content": {"type": "string"}},
                        ["path", "content"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = args["content"].encode("utf-8")
        try:
            await ctx.sandbox.write_file(args["path"], data)
        except OSError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        return ToolResult(content=f"wrote {len(data)} bytes to {args['path']}")


def _snippet(text: str, pos: int, new_len: int, context: int = 4) -> str:
    start_line = text.count("\n", 0, pos)
    end_line = text.count("\n", 0, pos + new_len)
    lines = text.splitlines()
    lo, hi = max(start_line - context, 0), min(end_line + context + 1, len(lines))
    return "\n".join(f"{i + 1:6}\t{lines[i]}" for i in range(lo, hi))


class EditFileTool:
    readonly = False
    spec = ToolSpec(
        name="edit_file",
        description=("Replace an exact string in a file. old must match exactly (including "
                     "whitespace and indentation) and be unique unless replace_all is true. "
                     "Returns the edited region with line numbers."),
        parameters=_obj({"path": {"type": "string"}, "old": {"type": "string"},
                         "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
                        ["path", "old", "new"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path, old, new = args["path"], args["old"], args["new"]
        if old == new:
            return ToolResult(content="error: old and new are identical", is_error=True)
        try:
            text = (await ctx.sandbox.read_file(path)).decode("utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError) as e:
            return ToolResult(content=f"error: cannot read {path}: {e}", is_error=True)
        if not old:
            return ToolResult(content="error: old must not be empty (use write_file)",
                              is_error=True)
        count = text.count(old)
        if count == 0:
            hint = ""
            stripped = old.strip()
            if stripped and stripped in text:
                hint = " (a whitespace-stripped version exists; check indentation)"
            return ToolResult(content=f"error: old string not found in {path}{hint}",
                              is_error=True)
        if count > 1 and not args.get("replace_all"):
            lines = [text.count("\n", 0, m.start()) + 1
                     for m in re.finditer(re.escape(old), text)]
            return ToolResult(content=(f"error: old string occurs {count} times in {path} "
                                       f"(lines {lines[:20]}); add surrounding context to make it "
                                       f"unique or set replace_all=true"), is_error=True)
        pos = text.find(old)
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        try:
            await ctx.sandbox.write_file(path, updated.encode("utf-8"))
        except OSError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        n = count if args.get("replace_all") else 1
        return ToolResult(content=f"replaced {n} occurrence(s) in {path}\n"
                                  + _snippet(updated, pos, len(new)))


class RecallTool:
    readonly = True
    spec = ToolSpec(
        name="recall",
        description=("Recover messages folded away by context compaction. recall(query=...) "
                     "searches compacted entries (case-insensitive substring) and lists matches; "
                     "recall(entry=n) returns entry n in full."),
        parameters=_obj({"query": {"type": "string"}, "entry": {"type": "integer"},
                         "max_results": {"type": "integer"}}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        st = ctx.state
        folded = st.entries[st.pinned: st.first_kept]
        if not folded:
            return ToolResult(content="nothing has been compacted yet", is_error=True)
        if args.get("entry") is not None:
            n = int(args["entry"])
            if not (st.pinned <= n < st.first_kept):
                return ToolResult(content=f"entry {n} is not in the compacted range "
                                          f"{st.pinned}..{st.first_kept - 1}", is_error=True)
            e = st.entries[n]
            body = e.msg.content
            if e.full_output_ref:
                body = ctx.blobs.get_text(e.full_output_ref)
            text = [f"[entry {n} · {e.kind} · {e.msg.role}]"]
            if e.msg.reasoning:
                text.append(f"[thinking]\n{e.msg.reasoning}")
            if body:
                text.append(body)
            for tc in e.msg.tool_calls:
                text.append(f"[tool call] {tc.name}({tc.arguments_raw})")
            return ToolResult(content="\n".join(text))
        q = (args.get("query") or "").lower()
        if not q:
            return ToolResult(content="provide query or entry", is_error=True)
        hits = []
        for e in folded:
            hay = [e.msg.content, e.msg.reasoning or ""] + [tc.arguments_raw
                                                            for tc in e.msg.tool_calls]
            if e.full_output_ref:
                hay.append(ctx.blobs.get_text(e.full_output_ref))
            for h in hay:
                i = h.lower().find(q)
                if i >= 0:
                    snippet = h[max(i - 150, 0): i + 250].replace("\n", " ")
                    hits.append(f"entry {e.idx} ({e.kind}): …{snippet}…")
                    break
            if len(hits) >= int(args.get("max_results") or 20):
                break
        return ToolResult(content="\n".join(hits) if hits else f"no match for {q!r}")


class ReadOutputTool:
    readonly = True
    spec = ToolSpec(
        name="read_output",
        description="Page through a truncated tool output by ref (0-based line offset).",
        parameters=_obj({"ref": {"type": "string"}, "offset": {"type": "integer"},
                         "limit": {"type": "integer"}}, ["ref"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        prefix = args["ref"].strip()
        match = next((e.full_output_ref for e in ctx.state.entries
                      if e.full_output_ref and e.full_output_ref.startswith(prefix)), None)
        if not match:
            return ToolResult(content=f"unknown ref {prefix!r}", is_error=True)
        offset = max(int(args.get("offset") or 0), 0)
        limit = max(int(args.get("limit") or 400), 1)
        total = ctx.blobs.line_count(match)
        text = ctx.blobs.read_slice(match, offset=offset, limit=limit)
        return ToolResult(content=f"[lines {offset}-{min(offset + limit, total) - 1} of {total}]\n"
                                  + text)


FINISH_SPEC = ToolSpec(
    name="finish",
    description=(
        "End this activation. outcome: completed (task done), blocked (cannot proceed; explain), "
        "need_input (a decision or information from the requester is required), handoff (stop "
        "and hand over remaining work). summary: what was done and the current state. "
        "artifacts: paths or identifiers of produced outputs. claims: factual statements you "
        "assert about the result (each should be checkable)."),
    parameters=_obj({
        "outcome": {"type": "string", "enum": ["completed", "blocked", "need_input", "handoff"]},
        "summary": {"type": "string"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "claims": {"type": "array", "items": {"type": "string"}},
    }, ["outcome", "summary"]),
)

BUILTIN_TOOLS = {t.spec.name: t for t in (BashTool(), ReadFileTool(), WriteFileTool(),
                                          EditFileTool(), RecallTool(), ReadOutputTool())}
