from sciteam.harness.config import HarnessConfig
from sciteam.harness.state import ActivationState
from sciteam.providers.llm.types import ChatMessage, ToolSpec

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier work in this activation was compacted to save context. The "
    "summary below replaces those messages; the task statement above is unchanged and still "
    "applies. Continue from where the summary leaves off.\n\n"
)


def harness_contract(cfg: HarnessConfig) -> str:
    b = cfg.budget
    return f"""

## Harness contract
- You act only through tool calls. Every activation must end with a call to `finish`; a reply without a tool call does not end the work.
- Limits for this activation: cost ${b.cost_usd:.0f}, wall clock {b.wall_clock_s / 3600:.1f} h, {b.max_steps} steps. These are generous safety limits, not targets: do the work thoroughly and verify it. You will be notified at {int(b.warn_at * 100)}% and {int(b.decide_at * 100)}% usage.
- Older messages may be replaced by a summary when the context grows; use `recall` to recover exact details instead of guessing.
- Long tool outputs are truncated in the middle; use `read_output` to see the omitted part."""  # noqa: E501


def assemble(system_prompt: str, state: ActivationState) -> list[ChatMessage]:
    msgs = [ChatMessage(role="system", content=system_prompt)]
    msgs += [e.msg for e in state.entries[: state.pinned]]
    if state.summary:
        msgs.append(ChatMessage(role="user", content=SUMMARY_PREFIX + state.summary.summary))
    msgs += [e.msg for e in state.entries[state.first_kept:]]
    return msgs


def fixed_tokens(system_prompt: str, tools: list[ToolSpec]) -> int:
    n = len(system_prompt) + sum(len(t.description) + len(str(t.parameters)) for t in tools)
    return n // 4 + 16
