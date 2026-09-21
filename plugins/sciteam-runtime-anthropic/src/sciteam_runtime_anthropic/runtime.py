"""Anthropic Claude RuntimePort implementation for SciTeam."""

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

DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicRuntimePort(RuntimePort):
    """RuntimePort adapter communicating with Anthropic's Messages API."""

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
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self._base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL)
        ).rstrip("/")
        self._max_tokens = int(max_tokens or os.environ.get("ANTHROPIC_MAX_TOKENS", "4096"))
        self._temperature = float(temperature or os.environ.get("ANTHROPIC_TEMPERATURE", "0.0"))
        self._http_transport = http_transport

    def version(self) -> str:
        """Report model version for provenance and runtime identity."""
        return f"anthropic:{self._model}"

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
            kind="anthropic_agent",
            input=task,
            agent_id=agent_id,
            parent_run_id=parent_run_id,
            metadata={"model": self._model, "depth": depth, "work_dir": work_dir},
        )

        default_prompt = "You are an institutional worker in a multi-agent system."
        system_prompt = ctx.get("system_prompt", default_prompt)
        messages: list[dict[str, Any]] = []

        history = ctx.get("history")
        if isinstance(history, list):
            for item in history:
                if isinstance(item, dict) and "role" in item and "content" in item:
                    messages.append({"role": item["role"], "content": item["content"]})

        messages.append({"role": "user", "content": task})

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "system": system_prompt,
            "messages": messages,
        }

        tools = ctx.get("tools")
        if isinstance(tools, list) and tools:
            payload["tools"] = [
                {
                    "name": t.get("name", "unnamed_tool"),
                    "description": t.get("summary", t.get("description", "")),
                    "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
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
            logger.warning("Anthropic runtime call failed: %s", exc)
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
                url=f"{self._base_url}/messages",
                code=401,
                msg="Unauthorized: ANTHROPIC_API_KEY is not set",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/messages",
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
        blocks = data.get("content", [])
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                    }
                )

        metadata_updates: dict[str, Any] = {}
        usage = data.get("usage", {})
        if usage:
            metadata_updates["usage"] = usage

        if tool_calls:
            metadata_updates["tool_calls"] = tool_calls
            if not text_parts:
                return json.dumps({"tool_calls": tool_calls}), metadata_updates

        return "\n".join(text_parts), metadata_updates

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
                diagnosis=f"Anthropic authentication failed (HTTP {status_code}): {error_body}",
                retry_hint="Verify that ANTHROPIC_API_KEY is valid and has access",
            )
        if status_code == 429:
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="RATE_LIMITED",
                diagnosis="Anthropic rate limit reached",
                retry_hint="Wait for quota reset and retry with exponential backoff",
            )
        if status_code in (500, 529):
            return AgentRunResult(
                run=run,
                output="",
                outcome=AgentOutcome.RECOVERABLE,
                reason_code="SERVER_OVERLOAD",
                diagnosis=f"Anthropic server temporarily overloaded (HTTP {status_code})",
                retry_hint="Retry after short delay",
            )
        return AgentRunResult(
            run=run,
            output="",
            outcome=AgentOutcome.FATAL,
            reason_code=f"HTTP_{status_code}",
            diagnosis=f"Anthropic API returned error {status_code}: {error_body}",
            retry_hint="Inspect request parameters and model compatibility",
        )
