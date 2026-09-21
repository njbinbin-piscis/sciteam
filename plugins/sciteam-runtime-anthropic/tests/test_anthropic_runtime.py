"""Tests for sciteam-runtime-anthropic plugin."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest
from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginRegistry
from sciteam.runtime import AgentOutcome, RunState
from sciteam_runtime_anthropic import AnthropicRuntimePort, register


def test_register_manifest_and_provider() -> None:
    registry = PluginRegistry()
    api = PluginAPI("anthropic_test", registry)
    manifest = register(api)

    assert manifest.id == "sciteam-runtime-anthropic"
    assert manifest.version == "0.1.0"
    assert manifest.sciteam_api_version == PLUGIN_API_VERSION
    assert "runtime" in manifest.provides

    factory = registry.runtime_port_factory("anthropic")
    assert factory is not None
    port = factory()
    assert isinstance(port, AnthropicRuntimePort)
    assert port.version() == "anthropic:claude-3-5-sonnet-20241022"


def test_custom_model_and_version() -> None:
    port = AnthropicRuntimePort(
        model="claude-3-7-sonnet-latest",
        api_key="test-key",
        max_tokens=8192,
        temperature=0.5,
    )
    assert port.version() == "anthropic:claude-3-7-sonnet-latest"
    assert port._max_tokens == 8192
    assert port._temperature == 0.5


@pytest.mark.asyncio
async def test_successful_text_execution() -> None:
    captured_payloads: list[dict[str, Any]] = []

    def fake_transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payloads.append(payload)
        return {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Auditing completed. No violations found."}],
            "usage": {"input_tokens": 150, "output_tokens": 42},
        }

    port = AnthropicRuntimePort(api_key="sk-test", http_transport=fake_transport)
    context = {
        "system_prompt": "You are the Auditor.",
        "history": [{"role": "user", "content": "hello"}],
    }
    result = await port.run_subagent(
        agent_id="auditor_seat",
        task="Audit the latest submission",
        work_dir="/tmp/work",
        context=context,
    )

    assert result.outcome == AgentOutcome.OK
    assert result.output == "Auditing completed. No violations found."
    assert result.run.state == RunState.SUCCEEDED
    assert result.run.spec.metadata["usage"] == {"input_tokens": 150, "output_tokens": 42}
    assert len(captured_payloads) == 1
    req = captured_payloads[0]
    assert req["system"] == "You are the Auditor."
    assert req["messages"][-1]["content"] == "Audit the latest submission"


@pytest.mark.asyncio
async def test_tool_use_response_parsing() -> None:
    def fake_transport(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "msg_tool_456",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_abc",
                    "name": "bash",
                    "input": {"command": "pytest -q"},
                }
            ],
            "usage": {"input_tokens": 200, "output_tokens": 30},
        }

    port = AnthropicRuntimePort(api_key="sk-test", http_transport=fake_transport)
    tools = [{"name": "bash", "summary": "Execute shell command", "parameters": {"type": "object"}}]
    result = await port.run_subagent(
        agent_id="implementer_seat",
        task="Run test suite",
        work_dir="/tmp/work",
        context={"tools": tools},
    )

    assert result.outcome == AgentOutcome.OK
    assert "tool_calls" in result.run.spec.metadata
    parsed_calls = result.run.spec.metadata["tool_calls"]
    assert len(parsed_calls) == 1
    assert parsed_calls[0]["name"] == "bash"
    assert parsed_calls[0]["input"] == {"command": "pytest -q"}


@pytest.mark.asyncio
async def test_auth_error_is_fatal() -> None:
    def fake_transport(payload: dict[str, Any]) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    # Mock read() on HTTPError
    class MockHTTPError(urllib.error.HTTPError):
        def read(self) -> bytes:
            return b'{"error": {"type": "authentication_error", "message": "invalid api key"}}'

    def transport_with_body(payload: dict[str, Any]) -> dict[str, Any]:
        raise MockHTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    port = AnthropicRuntimePort(api_key="bad-key", http_transport=transport_with_body)
    result = await port.run_subagent(agent_id="test", task="ping", work_dir="/tmp")

    assert result.outcome == AgentOutcome.FATAL
    assert result.reason_code == "AUTH_ERROR"
    assert result.run.state == RunState.FAILED


@pytest.mark.asyncio
async def test_rate_limit_and_overload_are_recoverable() -> None:
    class Mock429(urllib.error.HTTPError):
        def read(self) -> bytes:
            return b'{"error": {"type": "rate_limit_error", "message": "Quota exceeded"}}'

    class Mock529(urllib.error.HTTPError):
        def read(self) -> bytes:
            return b'{"error": {"type": "overloaded_error", "message": "Overloaded"}}'

    def transport_429(payload: dict[str, Any]) -> dict[str, Any]:
        raise Mock429("url", 429, "Too Many Requests", None, None)  # type: ignore[arg-type]

    def transport_529(payload: dict[str, Any]) -> dict[str, Any]:
        raise Mock529("url", 529, "Site Overloaded", None, None)  # type: ignore[arg-type]

    port_429 = AnthropicRuntimePort(api_key="key", http_transport=transport_429)
    res_429 = await port_429.run_subagent(agent_id="test", task="ping", work_dir="/tmp")
    assert res_429.outcome == AgentOutcome.RECOVERABLE
    assert res_429.reason_code == "RATE_LIMITED"

    port_529 = AnthropicRuntimePort(api_key="key", http_transport=transport_529)
    res_529 = await port_529.run_subagent(agent_id="test", task="ping", work_dir="/tmp")
    assert res_529.outcome == AgentOutcome.RECOVERABLE
    assert res_529.reason_code == "SERVER_OVERLOAD"


@pytest.mark.asyncio
async def test_transport_exception_is_recoverable() -> None:
    def broken_transport(payload: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionResetError("Connection dropped by peer")

    port = AnthropicRuntimePort(api_key="key", http_transport=broken_transport)
    result = await port.run_subagent(agent_id="test", task="ping", work_dir="/tmp")
    assert result.outcome == AgentOutcome.RECOVERABLE
    assert result.reason_code == "TRANSPORT_ERROR"
    assert "Connection dropped" in result.diagnosis
