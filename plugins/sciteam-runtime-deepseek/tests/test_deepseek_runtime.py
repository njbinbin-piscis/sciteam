"""Tests for sciteam-runtime-deepseek plugin."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest
from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginRegistry
from sciteam.runtime import AgentOutcome, RunState
from sciteam_runtime_deepseek import DeepSeekRuntimePort, register


def test_register_manifest_and_provider() -> None:
    registry = PluginRegistry()
    api = PluginAPI("deepseek_test", registry)
    manifest = register(api)

    assert manifest.id == "sciteam-runtime-deepseek"
    assert manifest.version == "0.1.0"
    assert manifest.sciteam_api_version == PLUGIN_API_VERSION
    assert "runtime" in manifest.provides

    factory = registry.runtime_port_factory("deepseek")
    assert factory is not None
    port = factory()
    assert isinstance(port, DeepSeekRuntimePort)
    assert port.version() == "deepseek:deepseek-chat"


def test_reasoner_model_version() -> None:
    port = DeepSeekRuntimePort(model="deepseek-reasoner", api_key="test-key")
    assert port.version() == "deepseek:deepseek-reasoner"


@pytest.mark.asyncio
async def test_successful_chat_execution() -> None:
    captured: list[dict[str, Any]] = []

    def fake_transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {
            "id": "chatcmpl-123",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Proposed hypothesis: institutional authority bounds drift.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20},
        }

    port = DeepSeekRuntimePort(api_key="sk-test", http_transport=fake_transport)
    result = await port.run_subagent(
        agent_id="proposer_seat",
        task="Formulate initial hypothesis",
        work_dir="/tmp/work",
    )

    assert result.outcome == AgentOutcome.OK
    assert "Proposed hypothesis" in result.output
    assert result.run.state == RunState.SUCCEEDED
    assert result.run.spec.metadata["usage"]["completion_tokens"] == 20
    assert captured[0]["model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_deepseek_reasoner_isolates_reasoning_content() -> None:
    def fake_transport(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "chatcmpl-r1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning_content": (
                            "Let's first analyze the institutional rules. "
                            "Member A holds no authority..."
                        ),
                        "content": "Final verdict: submission rejected due to unauthorized seat.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 90},
        }

    port = DeepSeekRuntimePort(
        model="deepseek-reasoner",
        api_key="sk-test",
        http_transport=fake_transport,
    )
    result = await port.run_subagent(
        agent_id="adjudicator_seat",
        task="Adjudicate submission",
        work_dir="/tmp/work",
    )

    assert result.outcome == AgentOutcome.OK
    # The final output contains the direct decision, not the thinking process
    assert result.output == "Final verdict: submission rejected due to unauthorized seat."
    # The thinking process is safely preserved in metadata for institutional auditing
    assert (
        result.run.spec.metadata["reasoning_content"]
        == "Let's first analyze the institutional rules. Member A holds no authority..."
    )


@pytest.mark.asyncio
async def test_function_calling_tool_calls_parsing() -> None:
    def fake_transport(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "chatcmpl-tool",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_manifest",
                                    "arguments": '{"path": "institution.yaml"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

    port = DeepSeekRuntimePort(api_key="sk-test", http_transport=fake_transport)
    tools = [
        {
            "name": "read_manifest",
            "summary": "Read institution manifest",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    result = await port.run_subagent(
        agent_id="worker",
        task="Read rules",
        work_dir="/tmp",
        context={"tools": tools},
    )

    assert result.outcome == AgentOutcome.OK
    assert "tool_calls" in result.run.spec.metadata
    calls = result.run.spec.metadata["tool_calls"]
    assert calls[0]["function"]["name"] == "read_manifest"


@pytest.mark.asyncio
async def test_insufficient_balance_402_is_fatal() -> None:
    class Mock402(urllib.error.HTTPError):
        def read(self) -> bytes:
            return b'{"error": {"code": "insufficient_balance", "message": "Balance depleted"}}'

    def transport_402(payload: dict[str, Any]) -> dict[str, Any]:
        raise Mock402("url", 402, "Payment Required", None, None)  # type: ignore[arg-type]

    port = DeepSeekRuntimePort(api_key="key", http_transport=transport_402)
    result = await port.run_subagent(agent_id="test", task="ping", work_dir="/tmp")

    assert result.outcome == AgentOutcome.FATAL
    assert result.reason_code == "INSUFFICIENT_BALANCE"
    assert "balance exhausted" in result.diagnosis


@pytest.mark.asyncio
async def test_auth_and_rate_limit_errors() -> None:
    class MockError(urllib.error.HTTPError):
        def __init__(self, code: int, body: bytes) -> None:
            super().__init__("url", code, "Error", None, None)  # type: ignore[arg-type]
            self._body = body

        def read(self) -> bytes:
            return self._body

    def transport_401(payload: dict[str, Any]) -> dict[str, Any]:
        raise MockError(401, b'{"error": "invalid api key"}')

    def transport_429(payload: dict[str, Any]) -> dict[str, Any]:
        raise MockError(429, b'{"error": "rate limit"}')

    port_401 = DeepSeekRuntimePort(api_key="bad", http_transport=transport_401)
    res_401 = await port_401.run_subagent(agent_id="test", task="ping", work_dir="/tmp")
    assert res_401.outcome == AgentOutcome.FATAL
    assert res_401.reason_code == "AUTH_ERROR"

    port_429 = DeepSeekRuntimePort(api_key="key", http_transport=transport_429)
    res_429 = await port_429.run_subagent(agent_id="test", task="ping", work_dir="/tmp")
    assert res_429.outcome == AgentOutcome.RECOVERABLE
    assert res_429.reason_code == "RATE_LIMITED"
