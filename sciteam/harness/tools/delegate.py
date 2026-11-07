from typing import Any

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.providers.llm.types import ToolSpec


class DelegateTool:
    readonly = False
    spec = ToolSpec(
        name="delegate",
        description=(
            "Hand a self-contained piece of work to a sub-agent with a fresh context: an "
            "independent recomputation, a search through code or data, a parameter sweep, a "
            "check you want done without your own assumptions. It works in the same sandbox "
            "with the tools you name (default: bash, file and search tools), on its own "
            "budget, and returns its summary to you. Write the task so it stands alone: the "
            "goal, the files and data involved, and what the summary must report."),
        parameters={"type": "object", "properties": {
            "task": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "string"}},
            "max_steps": {"type": "integer"},
            "tokens": {"type": "integer"}},
            "required": ["task"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parent = ctx.extras.get("harness")
        if parent is None:
            return ToolResult(content="error: sub-agents are not available here", is_error=True)
        d = parent.cfg.delegate
        if parent.delegations >= d.per_activation:
            return ToolResult(content=f"error: at most {d.per_activation} sub-agents per "
                                      "activation; do the rest yourself or hand off",
                              is_error=True)
        allowed = [t for t in d.tools if t in parent.available_tools and t != "delegate"]
        asked = list(args.get("tools") or allowed)
        bad = sorted(set(asked) - set(allowed))
        if bad:
            return ToolResult(content=f"error: a sub-agent can use {', '.join(allowed)}; "
                                      f"not {', '.join(bad)}", is_error=True)
        task = str(args["task"]).strip()
        if not task:
            return ToolResult(content="error: give the sub-agent a task", is_error=True)
        tokens = min(int(args.get("tokens") or d.tokens), d.tokens)
        steps = min(int(args.get("max_steps") or d.max_steps), d.max_steps)
        wall = min(d.wall_clock_s, ctx.remaining_wall_s)
        r = await parent.delegate(task, tools=asked, tokens=tokens, max_steps=steps,
                                  wall_clock_s=wall)
        used = r.totals.prompt_tokens + r.totals.completion_tokens
        head = (f"[sub-agent {r.activation_id}: {r.outcome}"
                f"{'/' + r.reason if r.reason else ''}; {r.steps} steps, {used} tokens]")
        body = r.summary.strip() or "(no summary)"
        if len(body) > d.summary_max_chars:
            body = body[: d.summary_max_chars] + " …[summary truncated]"
        parts = [head, body]
        if r.artifacts:
            parts.append("artifacts: " + ", ".join(r.artifacts))
        if r.claims:
            parts.append("claims:\n" + "\n".join(f"- {c}" for c in r.claims))
        return ToolResult(content="\n".join(parts), is_error=r.outcome == "error",
                          meta={"sub_agent": r.activation_id, "outcome": r.outcome,
                                "tokens": used, "cost_usd": r.totals.cost_usd,
                                "steps": r.steps})
