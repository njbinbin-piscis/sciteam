from typing import Any

from sciteam.harness.memory import MemoryView
from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.providers.llm.types import ToolSpec


def _view(ctx: ToolContext) -> MemoryView | None:
    return ctx.extras.get("memory")


class RememberTool:
    readonly = False
    spec = ToolSpec(
        name="remember",
        description=(
            "Record a lesson that should shape the rest of this project: a pitfall and how to "
            "avoid it, a tool argument that keeps going wrong, a convention the owner asked "
            "for, a result others will need. It is in force at once for everyone it applies "
            "to. scope: all (default), seat:<member or role>, or tool:<tool name> (shown when "
            "that tool fails; when is an optional regex its output must match). Keep it to "
            "one checkable sentence or two; if it replaces an earlier note, forget that one."),
        parameters={"type": "object", "properties": {
            "text": {"type": "string"}, "title": {"type": "string"},
            "scope": {"type": "string"}, "when": {"type": "string"}},
            "required": ["text"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        view = _view(ctx)
        if view is None:
            return ToolResult(content="error: this run keeps no memory", is_error=True)
        try:
            note = view.book.remember(
                str(args["text"]), scope=str(args.get("scope") or "all"),
                title=str(args.get("title") or ""), when=args.get("when") or None,
                author=view.member,
                sources=[{"project": view.project, "activation": view.activation}])
        except ValueError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        return ToolResult(content=f"remembered {note.id} (scope {note.scope})",
                          meta={"note": note.id})


class ForgetTool:
    readonly = False
    spec = ToolSpec(
        name="forget",
        description=("Drop a memory note (project or platform) that turned out wrong, outdated "
                     "or superseded, with the reason. It stops applying in this project; a "
                     "platform note you forget is reviewed when the project is accepted."),
        parameters={"type": "object", "properties": {
            "id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id", "reason"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        view = _view(ctx)
        if view is None:
            return ToolResult(content="error: this run keeps no memory", is_error=True)
        try:
            note = view.book.forget(str(args["id"]), reason=str(args["reason"]), by=view.member)
        except ValueError as e:
            return ToolResult(content=f"error: {e}", is_error=True)
        return ToolResult(content=f"forgot {note.id}", meta={"note": note.id})


MEMORY_TOOLS = {t.spec.name: t for t in (RememberTool(), ForgetTool())}
