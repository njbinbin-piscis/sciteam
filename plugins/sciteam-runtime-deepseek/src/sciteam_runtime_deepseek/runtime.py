"""DeepSeek RuntimePort implementation for SciTeam."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sciteam.runtime import (
    AgentOutcome,
    AgentRunResult,
    Run,
    RunSpec,
    RunState,
    RuntimePort,
)

logger = logging.getLogger(__name__)

DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekRuntimePort(RuntimePort):
    """RuntimePort adapter communicating with DeepSeek's OpenAI-compatible API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        http_transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._model = model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
        self._base_url = (
            base_url or os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
        ).rstrip("/")
        self._max_tokens = int(max_tokens or os.environ.get("DEEPSEEK_MAX_TOKENS", "4096"))
        self._temperature = float(temperature or os.environ.get("DEEPSEEK_TEMPERATURE", "0.0"))
        self._http_transport = http_transport

    def version(self) -> str:
        """Report model version for provenance and runtime identity."""
        return f"deepseek:{self._model}"

    async def run_subagent(
        self,
        *,
        agent_id: str,
        task: str,
        work_dir: str,
        parent_run_id: str | None = None,
        depth: int = 0,
        context: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        ctx = context or {}
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(UTC)

        spec = RunSpec(
            kind="deepseek_agent",
            input=task,
            agent_id=agent_id,
            parent_run_id=parent_run_id,
            metadata={"model": self._model, "depth": depth, "work_dir": work_dir},
        )

        messages: list[dict[str, Any]] = []
        default_prompt = "You are an institutional worker in a multi-agent system."
        system_prompt = ctx.get("system_prompt", default_prompt)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        history = ctx.get("history")
        if isinstance(history, list):
            for item in history:
                if isinstance(item, dict) and "role" in item and "content" in item:
                    messages.append({"role": item["role"], "content": item["content"]})

        messages.append({"role": "user", "content": task})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }

        tools = ctx.get("tools")
        if isinstance(tools, list) and tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", "unnamed_tool"),
                        "description": t.get("summary", t.get("description", "")),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
                for t in tools
                if isinstance(t, dict)
            ]

        try:
            response_data = await self._send_request(payload)
            output_text, metadata_updates = self._parse_response(response_data)
            spec.metadata.update(metadata_updates)

            run = Run(
                run_id=run_id,
                spec=spec,
                state=RunState.SUCCEEDED,
                created_at=created_at,
                updated_at=datetime.now(UTC),
                output=output_text,
            )
            return AgentRunResult(
                run=run,
                output=output_text,
                outcome=AgentOutcome.OK,
            )

        except urllib.error.HTTPError as exc:
            status_code = exc.code
            error_body = exc.read().decode("utf-8", errors="replace")
            return self._handle_http_error(run_id, spec, created_at, status_code, error_body)

        except Exception as exc:
            logger.warning("DeepSeek runtime call failed: %s", exc)
            run = Run(
                run_id=run_id,
                spec=spec,
                state=RunState.FAILED,
                created_at=created_at,
                updated_at=datetime.now(UTC),
                error=str(exc),
            )
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="TRANSPORT_ERROR",
                diagnosis=f"Connection error or network failure: {exc}",
                retry_hint="Retry the call with exponential backoff",
            )

    async def _send_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._http_transport is not None:
            result = self._http_transport(payload)
            if asyncio.iscoroutine(result):
                return await result
            return result

        if not self._api_key:
            raise urllib.error.HTTPError(
                url=f"{self._base_url}/chat/completions",
                code=401,
                msg="Unauthorized: DEEPSEEK_API_KEY is not set",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )

        def _do_request() -> dict[str, Any]:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)

        return await asyncio.to_thread(_do_request)

    def _parse_response(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        metadata_updates: dict[str, Any] = {}
        usage = data.get("usage", {})
        if usage:
            metadata_updates["usage"] = usage

        choices = data.get("choices", [])
        if not choices or not isinstance(choices[0], dict):
            return "", metadata_updates

        choice = choices[0]
        message = choice.get("message", {})

        reasoning = message.get("reasoning_content")
        if reasoning:
            metadata_updates["reasoning_content"] = reasoning

        tool_calls = message.get("tool_calls")
        if tool_calls:
            metadata_updates["tool_calls"] = tool_calls

        content = message.get("content") or ""

        if tool_calls and not content:
            return json.dumps({"tool_calls": tool_calls}), metadata_updates

        return content, metadata_updates

    def _handle_http_error(
        self,
        run_id: str,
        spec: RunSpec,
        created_at: datetime,
        status_code: int,
        error_body: str,
    ) -> AgentRunResult:
        run = Run(
            run_id=run_id,
            spec=spec,
            state=RunState.FAILED,
            created_at=created_at,
            updated_at=datetime.now(UTC),
            error=f"HTTP {status_code}: {error_body}",
        )

        if status_code in (401, 403):
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.FATAL,
                reason_code="AUTH_ERROR",
                diagnosis=f"DeepSeek authentication failed (HTTP {status_code}): {error_body}",
                retry_hint="Verify that DEEPSEEK_API_KEY is valid",
            )
        if status_code == 402:
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.FATAL,
                reason_code="INSUFFICIENT_BALANCE",
                diagnosis="DeepSeek account balance exhausted or insufficient",
                retry_hint="Top up DeepSeek account balance before continuing",
            )
        if status_code == 429:
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="RATE_LIMITED",
                diagnosis="DeepSeek rate limit reached",
                retry_hint="Wait for quota reset and retry with exponential backoff",
            )
        if status_code in (500, 502, 503, 504):
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="SERVER_ERROR",
                diagnosis=f"DeepSeek server returned error {status_code}",
                retry_hint="Retry after short delay",
            )
        return AgentRunResult(
            run=run,
            output="",
            outcome=AgentOutcome.FATAL,
            reason_code=f"HTTP_{status_code}",
            diagnosis=f"DeepSeek API returned HTTP {status_code}: {error_body}",
            retry_hint="Check model name and payload structure",
        )
