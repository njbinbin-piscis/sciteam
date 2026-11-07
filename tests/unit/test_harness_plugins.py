"""Unit tests for `sciteam/harness/plugins.py` (Task P1-1: HarnessPlugin & RTK Compaction)."""

from __future__ import annotations

import pytest

from sciteam.harness.config import (
    ExecutionProfilePolicy,
    HarnessConfig,
    RuntimePolicyConfig,
    RuntimePolicyToolOutput,
)
from sciteam.harness.loop import Harness
from sciteam.harness.plugins import (
    CONVERGENCE_PHASE_REMINDER,
    ECONOMY_PHASE_REMINDER,
    FINALIZE_DENIAL_REASON,
    BudgetPhaseHookPlugin,
    rtk_compact_text,
)
from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.events import EventStore
from sciteam.providers.llm.fakes import TEST_MODEL, ScriptedLlm, call
from sciteam.providers.llm.types import AssistantTurn
from sciteam.providers.sandbox.local import LocalSandbox


def test_rtk_compact_text_collapses_duplicate_lines_and_bounds_length():
    raw_lines = ["[BUILD] Compiling module.o..."] * 100 + ["[BUILD] Done."]
    raw_text = "\n".join(raw_lines)
    compacted = rtk_compact_text(raw_text, head_chars=2400, tail_chars=900)
    comp_lines = compacted.splitlines()
    assert len(comp_lines) == 3
    assert comp_lines[0] == "[BUILD] Compiling module.o..."
    assert comp_lines[1] == "[previous line repeated 99 more times]"
    assert comp_lines[2] == "[BUILD] Done."

    # Verify head/tail bounding when text exceeds head_chars + tail_chars
    long_unique = "\n".join(f"line_{i:04d}: " + ("x" * 40) for i in range(200))
    bounded = rtk_compact_text(long_unique, head_chars=200, tail_chars=100)
    assert "characters omitted; use the raw path for a targeted range" in bounded
    assert len(bounded) < 450


@pytest.mark.asyncio
async def test_harness_plugin_chain_economy_convergence_and_finalize_denial(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("line_same\n" * 50 + "final_line\n", encoding="utf-8")

    policy = RuntimePolicyConfig(
        default_profile="compact",
        active_profile="compact",
        tool_output=RuntimePolicyToolOutput(rtk_enabled=True, head_chars=2000, tail_chars=800),
        profiles={
            "compact": ExecutionProfilePolicy(
                reasoning_effort="low",
                economy_after_tools=1,
                strict_after_tools=2,
                finalize_after_tools=2,
                strict_reminder_every=1,
            )
        },
    )
    cfg = HarnessConfig(
        name="test-plugin-harness",
        model="scripted",
        tools=["read_file", "write_file", "finish"],
        runtime_policy=policy,
    )

    steps = [
        # Step 1: 1st tool call -> triggers economy_after_tools=1 reminder + RTK dedup
        AssistantTurn(
            content="Reading hello.txt (tool #1)",
            tool_calls=[call("read_file", path="hello.txt")],
        ),
        # Step 2: 2nd tool call -> triggers strict_after_tools=2 convergence reminder
        AssistantTurn(
            content="Writing out.txt (tool #2)",
            tool_calls=[call("write_file", path="out.txt", content="ok")],
        ),
        # Step 3: 3rd tool call -> exceeds finalize_after_tools=2, denied by PreToolUse hook!
        AssistantTurn(
            content="Reading hello.txt again (tool #3 - should be denied)",
            tool_calls=[call("read_file", path="hello.txt")],
        ),
        # Step 4: finish is still allowed after finalize_after_tools
        AssistantTurn(
            content="Finishing within budget",
            tool_calls=[call("finish", outcome="completed", summary="done", artifacts=["out.txt"])],
        ),
    ]

    store = EventStore(tmp_path / "events.db")
    blobs = BlobStore(tmp_path / "blobs")
    sb = LocalSandbox(ws)
    await sb.start()
    plugin = BudgetPhaseHookPlugin()
    llm = ScriptedLlm(steps, TEST_MODEL)
    try:
        h = Harness(
            cfg,
            model_info=TEST_MODEL,
            llm=llm,
            sandbox=sb,
            store=store,
            blobs=blobs,
            system_prompt="You are a test agent.",
            plugins=[plugin],
        )
        res = await h.run("Test task")
    finally:
        await sb.close()
        store.close()

    assert res.outcome == "completed"
    assert plugin.tool_count == 3
    assert plugin.economy_reminders_sent == 1
    assert plugin.convergence_reminders_sent == 1
    assert plugin.denied_calls == 1
    assert plugin.rtk_chars_saved > 0

    # Verify system prompt prefix remained 100% identical across all 4 LLM calls
    sys_prompts = [req[0][0].content for req in llm.requests]
    assert len(set(sys_prompts)) == 1

    # Verify tool outputs received by the LLM in subsequent turns contain the reminders and denial
    turn2_msgs = llm.requests[1][0]
    tool1_msg = next(m for m in turn2_msgs if m.role == "tool")
    assert ECONOMY_PHASE_REMINDER in tool1_msg.content
    assert "[previous line repeated" in tool1_msg.content

    turn3_msgs = llm.requests[2][0]
    tool2_Msgs = [m for m in turn3_msgs if m.role == "tool"]
    assert CONVERGENCE_PHASE_REMINDER in tool2_Msgs[-1].content

    turn4_msgs = llm.requests[3][0]
    tool3_Msgs = [m for m in turn4_msgs if m.role == "tool"]
    assert FINALIZE_DENIAL_REASON in tool3_Msgs[-1].content
    assert h.prefix_cache_violations == 0


def test_pre_finish_does_not_judge_summary_text():
    """Acceptance belongs to the institution and its reviewers, not to string checks."""
    from sciteam.harness.state import ActivationState

    plugin = BudgetPhaseHookPlugin()
    st = ActivationState()
    for summary in ("unknown", " ".join(["the opening to the open"] * 12), "Fitted Ea=0.42 eV"):
        assert plugin.pre_finish({"outcome": "completed", "summary": summary}, st, None) is None

