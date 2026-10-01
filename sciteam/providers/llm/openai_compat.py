"""OpenAI-compatible chat completions adapter (DeepSeek thinking mode included)."""

import asyncio
import json
import os
from typing import Any

import httpx

from sciteam.providers.llm.types import (
    AssistantTurn,
    AuthError,
    BadRequest,
    CallParams,
    ChatMessage,
    ContextOverflow,
    LlmError,
    ModelInfo,
    QuotaExhausted,
    RateLimited,
    ToolCall,
    ToolSpec,
    Transient,
    Usage,
)

_OVERFLOW_MARKERS = ("context length", "maximum context", "too long", "context_length", "exceeds")
# DeepSeek answers an empty balance with 402; some gateways use 400/429 with these bodies.
_QUOTA_MARKERS = ("insufficient balance", "insufficient_quota", "exceeded your current quota")


def message_to_wire(m: ChatMessage, *, passback_reasoning: bool) -> dict[str, Any]:
    d: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.role == "assistant":
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments_raw},
                }
                for tc in m.tool_calls
            ]
        if passback_reasoning and m.reasoning is not None:
            d["reasoning_content"] = m.reasoning
    if m.role == "tool":
        d["tool_call_id"] = m.tool_call_id
    return d


def tool_to_wire(t: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
    }


class StreamAccumulator:
    """Folds streamed chat.completion.chunk objects into one AssistantTurn."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.saw_reasoning = False
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.model = ""

    def feed(self, chunk: dict[str, Any]) -> None:
        if "error" in chunk:
            raise Transient(f"stream error: {chunk['error']}")
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.content.append(delta["content"])
            rc = delta.get("reasoning_content")
            if rc is not None:
                self.saw_reasoning = True
                self.reasoning.append(rc)
            for tc in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": []})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def result(self) -> AssistantTurn:
        calls = []
        for idx in sorted(self.calls):
            slot = self.calls[idx]
            raw = "".join(slot["args"])
            try:
                parsed = json.loads(raw) if raw.strip() else {}
                if not isinstance(parsed, dict):
                    parsed = None
            except json.JSONDecodeError:
                parsed = None
            calls.append(
                ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments_raw=raw,
                         arguments=parsed)
            )
        u = self.usage or {}
        usage = Usage(
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            cache_hit_tokens=u.get("prompt_cache_hit_tokens",
                                   (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            cache_miss_tokens=u.get("prompt_cache_miss_tokens", 0),
            reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            estimated=self.usage is None,
        )
        return AssistantTurn(
            content="".join(self.content),
            reasoning="".join(self.reasoning) if self.saw_reasoning else None,
            tool_calls=calls,
            finish_reason=self.finish_reason or "error",
            usage=usage,
            model=self.model,
        )


def classify_http_error(status: int, body: str, retry_after: str | None) -> LlmError:
    ra = None
    if retry_after:
        try:
            ra = float(retry_after)
        except ValueError:
            ra = None
    low = body.lower()
    if status == 402 or any(m in low for m in _QUOTA_MARKERS):
        return QuotaExhausted(body[:500], status=status)
    if status == 429:
        return RateLimited(body[:500], status=status, retry_after=ra)
    if status in (401, 403):
        return AuthError(body[:500], status=status)
    if status == 400 or status == 413:
        low = body.lower()
        if any(m in low for m in _OVERFLOW_MARKERS) and ("token" in low or "context" in low):
            return ContextOverflow(body[:500], status=status)
        return BadRequest(body[:2000], status=status)
    if status >= 500 or status in (408, 409):
        return Transient(body[:500], status=status)
    return BadRequest(body[:2000], status=status)


class OpenAICompatProvider:
    def __init__(self, model_info: ModelInfo, *, api_key: str | None = None,
                 client: httpx.AsyncClient | None = None, raw_sink=None):
        self.model_info = model_info
        self._api_key = api_key or os.environ.get(model_info.api_key_env, "")
        self._client = client
        self._raw_sink = raw_sink  # callable(str) -> BlobRef, optional

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=60.0),
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=64),
            )
        return self._client

    def build_body(self, messages: list[ChatMessage], tools: list[ToolSpec],
                   params: CallParams) -> dict[str, Any]:
        mi = self.model_info
        passback = mi.reasoning.supported and mi.reasoning.passback_with_tools
        body: dict[str, Any] = {
            "model": mi.wire_model,
            "messages": [message_to_wire(m, passback_reasoning=passback) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = [tool_to_wire(t) for t in tools]
            body["tool_choice"] = params.tool_choice
        if mi.reasoning.supported:
            body["thinking"] = {"type": "enabled"}
            effort = params.reasoning_effort or mi.reasoning.default_effort
            if effort:
                body["reasoning_effort"] = effort
        if params.max_output_tokens:
            body["max_tokens"] = params.max_output_tokens
        if params.seed is not None:
            body["seed"] = params.seed
        return body

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec], *,
                       params: CallParams) -> AssistantTurn:
        body = self.build_body(messages, tools, params)
        try:
            return await asyncio.wait_for(self._stream(body), timeout=params.timeout_s)
        except TimeoutError as e:
            raise Transient(f"call exceeded {params.timeout_s}s") from e

    async def _stream(self, body: dict[str, Any]) -> AssistantTurn:
        url = self.model_info.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        acc = StreamAccumulator()
        raw_lines: list[str] | None = [] if self._raw_sink else None
        try:
            async with self._get_client().stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise classify_http_error(resp.status_code, text,
                                              resp.headers.get("retry-after"))
                async for line in resp.aiter_lines():
                    if raw_lines is not None:
                        raw_lines.append(line)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    acc.feed(chunk)
        except (httpx.TransportError, httpx.StreamError) as e:
            raise Transient(f"{type(e).__name__}: {e}") from e
        if acc.finish_reason is None:
            raise Transient("stream ended without finish_reason")
        turn = acc.result()
        if raw_lines is not None:
            turn.raw_ref = self._raw_sink("\n".join(raw_lines))
        return turn

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
