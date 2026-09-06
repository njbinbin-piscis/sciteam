"""OpenAI-compatible chat client (stdlib only; no product dependencies).

Configuration via environment:
    SCITEAM_LLM_BASE_URL   e.g. https://api.example.com/v1
    SCITEAM_LLM_API_KEY
    SCITEAM_LLM_MODEL      wire model name (free text, e.g. deepseek-chat)
    SCITEAM_LLM_TIMEOUT    seconds (default 120)
    SCITEAM_LLM_MAX_TOKENS default completion max_tokens (default 32768)
    SCITEAM_LLM_CONTEXT_WINDOW context budget hint in tokens (default 1000000)

Token usage is accumulated on the client so campaign budget clocks can charge it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import sciteam.limits as limits
import sciteam.pricing as pricing


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    max_retries: int = 3
    default_max_tokens: int = limits.MAX_TOKENS
    context_window: int = limits.CONTEXT_WINDOW

    @classmethod
    def from_env(cls) -> LlmConfig:
        base_url = os.environ.get("SCITEAM_LLM_BASE_URL", "").rstrip("/")
        api_key = os.environ.get("SCITEAM_LLM_API_KEY", "")
        model = os.environ.get("SCITEAM_LLM_MODEL", "")
        if not (base_url and api_key and model):
            raise LlmError(
                "missing LLM config: set SCITEAM_LLM_BASE_URL, SCITEAM_LLM_API_KEY, SCITEAM_LLM_MODEL"
            )
        timeout = float(os.environ.get("SCITEAM_LLM_TIMEOUT", "120"))
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout,
            default_max_tokens=limits.max_tokens(),
            context_window=limits.context_window(),
        )


@dataclass
class LlmUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        self.requests += 1
        if not usage:
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)

    @property
    def cost_usd(self) -> float:
        return pricing.cost_usd(
            prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens
        )


@dataclass(frozen=True)
class LlmResponse:
    content: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    message: dict[str, Any] = field(default_factory=dict)


class LlmClient:
    def __init__(self, config: LlmConfig | None = None) -> None:
        self.config = config or LlmConfig.from_env()
        self.usage = LlmUsage()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LlmResponse:
        mt = self.config.default_max_tokens if max_tokens is None else int(max_tokens)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max(1, mt),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if response_format is not None:
            payload["response_format"] = response_format
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.config.base_url}/chat/completions"
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                choices = raw.get("choices") or []
                if not choices:
                    raise LlmError(f"empty choices in response: {raw}")
                message = choices[0].get("message") or {}
                if not isinstance(message, dict):
                    message = {}
                content = str(message.get("content") or "")
                if not content.strip():
                    # Reasoning models may spend the budget on reasoning_content.
                    content = str(message.get("reasoning_content") or "")
                tool_calls = message.get("tool_calls") or []
                if not isinstance(tool_calls, list):
                    tool_calls = []
                usage = raw.get("usage") or {}
                self.usage.add(usage)
                return LlmResponse(
                    content=content,
                    usage=usage,
                    raw=raw,
                    tool_calls=tool_calls,
                    message=message,
                )
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.config.max_retries:
                    time.sleep(min(30.0, 2.0**attempt))
                    continue
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:  # noqa: BLE001
                    pass
                raise LlmError(f"HTTP {exc.code} from LLM API: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    time.sleep(min(30.0, 2.0**attempt))
                    continue
                raise LlmError(f"LLM API request failed: {exc}") from exc
        raise LlmError(f"LLM API request failed after retries: {last_error}")

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LlmResponse:
        return await asyncio.to_thread(
            self.complete,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
        )
