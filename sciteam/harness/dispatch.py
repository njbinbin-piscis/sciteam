import asyncio
import time
from collections.abc import Callable

from pydantic import BaseModel

from sciteam.harness.tools.base import Tool, ToolArgError, ToolContext, ToolResult, validate_args
from sciteam.providers.llm.types import ToolCall


class CallResult(BaseModel):
    call: ToolCall
    result: ToolResult
    duration_s: float


async def _run_one(tool: Tool, call: ToolCall, ctx: ToolContext) -> CallResult:
    t0 = time.monotonic()
    try:
        res = await tool.run(call.arguments or {}, ctx)
    except Exception as e:  # a tool bug must not kill the activation
        res = ToolResult(content=f"error: tool {call.name} raised {type(e).__name__}: {e}",
                         is_error=True, meta={"exception": type(e).__name__})
    return CallResult(call=call, result=res, duration_s=time.monotonic() - t0)


def precheck(call: ToolCall, tools: dict[str, Tool]) -> ToolResult | None:
    if call.name not in tools:
        return ToolResult(content=f"error: unknown tool '{call.name}'. Available: "
                                  f"{', '.join(sorted(tools))}, finish", is_error=True)
    if call.arguments is None:
        return ToolResult(content=(f"error: arguments are not a valid JSON object: "
                                   f"{call.arguments_raw[:500]!r}. Schema: "
                                   f"{tools[call.name].spec.parameters}"), is_error=True)
    try:
        validate_args(tools[call.name].spec, call.arguments)
    except ToolArgError as e:
        return ToolResult(content=f"error: {e}. Schema: {tools[call.name].spec.parameters}",
                          is_error=True)
    return None


async def dispatch(calls: list[ToolCall], tools: dict[str, Tool], ctx: ToolContext, *,
                   parallel_readonly: int,
                   on_start: Callable[[ToolCall], None] | None = None) -> list[CallResult]:
    """Contiguous readonly calls run concurrently; side-effect calls run alone, in order.
    Every call gets a result; a failing call does not stop later calls."""
    results: list[CallResult | None] = [None] * len(calls)
    sem = asyncio.Semaphore(max(parallel_readonly, 1))

    async def guarded(i: int) -> None:
        async with sem:
            results[i] = await _run_one(tools[calls[i].name], calls[i], ctx)

    batch: list[int] = []

    async def flush() -> None:
        if batch:
            await asyncio.gather(*(guarded(i) for i in batch))
            batch.clear()

    for i, call in enumerate(calls):
        bad = precheck(call, tools)
        if bad is not None:
            results[i] = CallResult(call=call, result=bad, duration_s=0.0)
            continue
        if on_start:
            on_start(call)
        if tools[call.name].readonly:
            batch.append(i)
            continue
        await flush()
        results[i] = await _run_one(tools[call.name], call, ctx)
    await flush()
    return [r for r in results if r is not None]
