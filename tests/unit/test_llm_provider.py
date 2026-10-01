import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from sciteam.kernel import BlobStore
from sciteam.providers.llm import (
    AssistantTurn,
    AuthError,
    BadRequest,
    CallParams,
    ChatMessage,
    ContextOverflow,
    QuotaExhausted,
    RateLimited,
    ToolSpec,
    Transient,
    Usage,
    load_models,
)
from sciteam.providers.llm.fakes import RecordingLlm, ReplayLlm, ScriptedLlm, call
from sciteam.providers.llm.openai_compat import (
    OpenAICompatProvider,
    StreamAccumulator,
    classify_http_error,
)
from sciteam.providers.llm.pricing import cost_usd, is_peak

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/deepseek_stream_tools.sse"
MODELS = load_models(ROOT / "configs/models.yaml")
ADD = ToolSpec(name="add", description="add", parameters={"type": "object"})


def _chunks(path: Path):
    for line in path.read_text().splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            yield json.loads(line[5:])


def test_accumulator_real_stream():
    acc = StreamAccumulator()
    for c in _chunks(FIXTURE):
        acc.feed(c)
    turn = acc.result()
    assert turn.finish_reason == "tool_calls"
    assert [tc.name for tc in turn.tool_calls] == ["add", "add"]
    assert [tc.arguments for tc in turn.tool_calls] == [{"a": 2, "b": 3}, {"a": 4, "b": 5}]
    assert all(tc.id.startswith("call_") for tc in turn.tool_calls)
    assert turn.reasoning
    assert turn.usage.prompt_tokens > 0 and not turn.usage.estimated
    assert turn.usage.cache_hit_tokens + turn.usage.cache_miss_tokens == turn.usage.prompt_tokens


def test_accumulator_bad_json_args_kept_raw():
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "x", "arguments": '{"a": '}}]}}]})
    acc.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    tc = acc.result().tool_calls[0]
    assert tc.arguments is None and tc.arguments_raw == '{"a": '
    assert acc.result().usage.estimated


@pytest.mark.parametrize(
    "status,body,cls",
    [
        (429, "slow down", RateLimited),
        (503, "busy", Transient),
        (401, "bad key", AuthError),
        (400, "This model's maximum context length is 1048576 tokens", ContextOverflow),
        (400, "invalid tool_choice", BadRequest),
        (402, '{"error":{"message":"Insufficient Balance"}}', QuotaExhausted),
        (400, '{"error":{"message":"Insufficient Balance"}}', QuotaExhausted),
    ],
)
def test_classify(status, body, cls):
    assert isinstance(classify_http_error(status, body, None), cls)


def test_retry_after_parsed():
    assert classify_http_error(429, "", "7").retry_after == 7.0


def test_pricing_peak_offpeak():
    m = MODELS["deepseek-flash"]
    u = Usage(prompt_tokens=1_000_000, cache_hit_tokens=800_000, cache_miss_tokens=200_000,
              completion_tokens=100_000)
    peak = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)       # Wednesday 02:00 UTC
    off = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)
    weekend = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)    # Saturday
    assert is_peak(m, peak) and not is_peak(m, off) and not is_peak(m, weekend)
    assert cost_usd(u, m, peak) == pytest.approx(0.8 * 0.006 + 0.2 * 0.30 + 0.1 * 1.20)
    assert cost_usd(u, m, off) == pytest.approx(cost_usd(u, m, peak) / 2)


def test_pricing_missing_cache_miss_field():
    m = MODELS["deepseek-flash"]
    u = Usage(prompt_tokens=1_000_000, cache_hit_tokens=0, completion_tokens=0)
    assert cost_usd(u, m, datetime(2026, 9, 23, 5, tzinfo=UTC)) == pytest.approx(0.15)


def test_body_passes_back_reasoning():
    p = OpenAICompatProvider(MODELS["deepseek-flash"], api_key="k")
    msgs = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="", reasoning="think", tool_calls=[call("add", a=1)]),
        ChatMessage(role="tool", content="1", tool_call_id=call("add", a=1).id),
    ]
    body = p.build_body(msgs, [ADD], CallParams(reasoning_effort="max"))
    assert body["messages"][1]["reasoning_content"] == "think"
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert body["messages"][2]["tool_call_id"] == call("add", a=1).id
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "max"


async def test_provider_over_mock_transport():
    payload = FIXTURE.read_bytes()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        p = OpenAICompatProvider(MODELS["deepseek-flash"], api_key="k", client=client)
        turn = await p.complete([ChatMessage(role="user", content="x")], [ADD],
                                params=CallParams())
    assert len(turn.tool_calls) == 2


async def test_provider_http_error_mapped():
    transport = httpx.MockTransport(lambda req: httpx.Response(429, text="rl",
                                                               headers={"retry-after": "3"}))
    async with httpx.AsyncClient(transport=transport) as client:
        p = OpenAICompatProvider(MODELS["deepseek-flash"], api_key="k", client=client)
        with pytest.raises(RateLimited):
            await p.complete([ChatMessage(role="user", content="x")], [], params=CallParams())


async def test_truncated_stream_is_transient():
    body = b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        p = OpenAICompatProvider(MODELS["deepseek-flash"], api_key="k", client=client)
        with pytest.raises(Transient):
            await p.complete([ChatMessage(role="user", content="x")], [], params=CallParams())


async def test_scripted_reports_usage():
    llm = ScriptedLlm([AssistantTurn(content="hello")])
    turn = await llm.complete([ChatMessage(role="user", content="x" * 400)], [],
                              params=CallParams())
    assert turn.usage.prompt_tokens > 0 and turn.usage.completion_tokens > 0


async def test_record_replay(tmp_path):
    blobs = BlobStore(tmp_path)
    rec = RecordingLlm(ScriptedLlm([AssistantTurn(content="a"), AssistantTurn(content="b")]), blobs)
    m1 = [ChatMessage(role="user", content="1")]
    m2 = m1 + [ChatMessage(role="assistant", content="a"), ChatMessage(role="user", content="2")]
    await rec.complete(m1, [], params=CallParams())
    await rec.complete(m2, [], params=CallParams())
    rep = ReplayLlm(blobs, rec.index)
    assert (await rep.complete(m1, [], params=CallParams())).content == "a"
    with pytest.raises(AssertionError):
        await rep.complete(m1, [], params=CallParams())
