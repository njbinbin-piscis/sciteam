import json
from collections.abc import Callable, Sequence

from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.canonical import canonical_json, sha256_hex
from sciteam.providers.llm.types import (
    AssistantTurn,
    CallParams,
    ChatMessage,
    LlmProvider,
    ModelInfo,
    PriceTier,
    ToolCall,
    ToolSpec,
    Usage,
)

TEST_MODEL = ModelInfo(
    name="scripted",
    provider="scripted",
    base_url="",
    api_key_env="",
    wire_model="scripted",
    context_window=200_000,
    max_output_tokens=32_000,
    pricing_usd_per_mtok={"flat": PriceTier(cache_hit=0.1, cache_miss=1.0, output=2.0)},
)


def estimate_tokens(messages: Sequence[ChatMessage]) -> int:
    total = 0
    for m in messages:
        total += len(m.content) + len(m.reasoning or "")
        total += sum(len(tc.arguments_raw) + len(tc.name) for tc in m.tool_calls)
    return total // 4 + 4 * len(messages)


def call(name: str, **args) -> ToolCall:
    raw = json.dumps(args)
    return ToolCall(id=f"call_{sha256_hex(name + raw)[:8]}", name=name, arguments_raw=raw,
                    arguments=args)


Step = AssistantTurn | Callable[[list[ChatMessage], list[ToolSpec]], AssistantTurn] | BaseException


class ScriptedLlm:
    """Returns scripted turns in order; always reports non-empty usage."""

    def __init__(self, steps: Sequence[Step], model_info: ModelInfo = TEST_MODEL):
        self.model_info = model_info
        self._steps = list(steps)
        self.requests: list[tuple[list[ChatMessage], list[ToolSpec], CallParams]] = []

    async def complete(self, messages, tools, *, params):
        self.requests.append((list(messages), list(tools), params))
        if not self._steps:
            raise AssertionError("ScriptedLlm ran out of steps")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        turn = step(messages, tools) if callable(step) else step.model_copy(deep=True)
        if turn.usage.prompt_tokens == 0:
            pt = estimate_tokens(messages)
            ct = max(1, (len(turn.content) + len(turn.reasoning or "")) // 4)
            turn.usage = Usage(prompt_tokens=pt, completion_tokens=ct, cache_miss_tokens=pt)
        turn.model = turn.model or self.model_info.name
        return turn


class RecordingLlm:
    """Wraps a provider; stores each request/response pair so the run can be replayed offline."""

    def __init__(self, inner: LlmProvider, blobs: BlobStore, index: list[dict] | None = None):
        self.inner = inner
        self.model_info = inner.model_info
        self.blobs = blobs
        self.index = index if index is not None else []

    async def complete(self, messages, tools, *, params):
        turn = await self.inner.complete(messages, tools, params=params)
        req = canonical_json({
            "messages": [m.model_dump(mode="json") for m in messages],
            "tools": [t.model_dump(mode="json") for t in tools],
        })
        self.index.append({
            "request": self.blobs.put_text(req).sha256,
            "response": self.blobs.put_text(turn.model_dump_json()).sha256,
        })
        return turn


class ReplayLlm:
    """Replays a RecordingLlm index; fails if the request differs from the recorded one."""

    def __init__(self, blobs: BlobStore, index: list[dict], model_info: ModelInfo = TEST_MODEL):
        self.model_info = model_info
        self.blobs = blobs
        self.index = list(index)

    async def complete(self, messages, tools, *, params):
        if not self.index:
            raise AssertionError("replay exhausted")
        entry = self.index.pop(0)
        req = canonical_json({
            "messages": [m.model_dump(mode="json") for m in messages],
            "tools": [t.model_dump(mode="json") for t in tools],
        })
        if self.blobs.put_text(req).sha256 != entry["request"]:
            raise AssertionError("replay diverged: request differs from recording")
        return AssistantTurn.model_validate_json(self.blobs.get_text(entry["response"]))
