import os
from pathlib import Path

import pytest

from sciteam.providers.llm import CallParams, ChatMessage, ToolSpec, load_models
from sciteam.providers.llm.openai_compat import OpenAICompatProvider

ROOT = Path(__file__).resolve().parents[2]
MODELS = load_models(ROOT / "configs/models.yaml")
ADD = ToolSpec(
    name="add",
    description="Add two integers.",
    parameters={"type": "object", "properties": {"a": {"type": "integer"},
                                                  "b": {"type": "integer"}},
                "required": ["a", "b"]},
)
FINISH = ToolSpec(
    name="finish",
    description="Report the final answer.",
    parameters={"type": "object", "properties": {"answer": {"type": "string"}},
                "required": ["answer"]},
)

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="no key")]


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
async def test_two_turn_tools_with_reasoning_passback(model):
    p = OpenAICompatProvider(MODELS[model])
    msgs = [ChatMessage(role="user", content="Use the add tool to compute 17+25, then finish.")]
    t1 = await p.complete(msgs, [ADD, FINISH], params=CallParams(reasoning_effort="low"))
    assert t1.tool_calls and t1.reasoning is not None
    assert t1.usage.prompt_tokens > 0 and not t1.usage.estimated
    msgs.append(ChatMessage(role="assistant", content=t1.content, reasoning=t1.reasoning,
                            tool_calls=t1.tool_calls))
    for tc in t1.tool_calls:
        a = tc.arguments or {}
        msgs.append(ChatMessage(role="tool", tool_call_id=tc.id,
                                content=str(a.get("a", 0) + a.get("b", 0))))
    t2 = await p.complete(msgs, [ADD, FINISH], params=CallParams(reasoning_effort="low"))
    assert t2.finish_reason in ("tool_calls", "stop")
    await p.aclose()


async def test_forced_tool_choice_rejected_in_thinking_mode():
    """Guards models.yaml `forced_tool_choice: false`; if this starts passing, update the catalog."""
    from sciteam.providers.llm import BadRequest

    assert MODELS["deepseek-flash"].forced_tool_choice is False
    p = OpenAICompatProvider(MODELS["deepseek-flash"])
    msgs = [ChatMessage(role="user", content="Say hello.")]
    with pytest.raises(BadRequest):
        await p.complete(msgs, [FINISH], params=CallParams(reasoning_effort="low",
                                                           tool_choice="required"))
    await p.aclose()
