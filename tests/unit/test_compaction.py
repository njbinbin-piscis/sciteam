from sciteam.harness.compaction.cut import find_cut_point, tail_budget
from sciteam.harness.compaction.extract import (
    build_anchor_index,
    build_verbatim_user_section,
    extract_file_ops,
    fallback_summary,
    format_file_ops,
    serialize_conversation,
)
from sciteam.harness.state import ActivationState
from sciteam.providers.llm.fakes import call
from sciteam.providers.llm.types import ChatMessage


def _state(n_rounds: int, tool_chars: int = 400) -> ActivationState:
    st = ActivationState()
    st.add_entry("task", ChatMessage(role="user", content="fix the bug"))
    st.pinned = 1
    for i in range(n_rounds):
        c = call("read_file", path=f"pkg/mod{i}.py")
        st.add_entry("model", ChatMessage(role="assistant", content=f"step {i}", reasoning="r" * 50,
                                          tool_calls=[c]))
        st.add_entry("tool", ChatMessage(role="tool", tool_call_id=c.id,
                                         content=f"out{i} " + "x" * tool_chars))
    return st


def test_tail_budget_lean_rules():
    assert tail_budget(1_048_576, fraction=0.025, floor=10_000, cap=25_000,
                       max_window_fraction=0.2) == 25_000
    assert tail_budget(200_000, fraction=0.025, floor=10_000, cap=25_000,
                       max_window_fraction=0.2) == 10_000
    assert tail_budget(16_000, fraction=0.025, floor=10_000, cap=25_000,
                       max_window_fraction=0.2) == 3200


def test_cut_never_on_tool_result_and_keeps_budget():
    st = _state(40)
    for keep in (50, 300, 1000, 3000, 10_000):
        cut = find_cut_point(st.entries, 1, len(st.entries), keep)
        assert st.entries[cut.first_kept].msg.role != "tool"
        kept = sum(e.tokens for e in st.entries[cut.first_kept:])
        total = sum(e.tokens for e in st.entries[1:])
        assert kept <= max(keep, 0) + 200 or kept == total


def test_single_task_is_not_split_turn():
    st = _state(20)
    cut = find_cut_point(st.entries, 1, len(st.entries), 500)
    assert not cut.is_split_turn  # pinned task sits outside the compactable region


def test_split_turn_with_incoming_message():
    st = _state(3)
    st.add_entry("incoming", ChatMessage(role="user", content="also handle the edge case"))
    for i in range(10):
        c = call("bash", command=f"run {i}")
        st.add_entry("model", ChatMessage(role="assistant", content="", tool_calls=[c]))
        st.add_entry("tool", ChatMessage(role="tool", tool_call_id=c.id, content="y" * 400))
    cut = find_cut_point(st.entries, 1, len(st.entries), 300)
    assert cut.is_split_turn and st.entries[cut.turn_start].kind == "incoming"
    assert cut.turn_start < cut.first_kept


def test_serialize_truncates_tool_and_thinking():
    st = _state(1, tool_chars=5000)
    st.entries[1].msg.reasoning = "t" * 5000
    text = serialize_conversation(st.entries, tool_chars=2000, thinking_chars=1000)
    assert "[Assistant thinking]" in text and "more characters truncated" in text
    assert len(text) < 5000


def test_file_ops_accumulate():
    st = _state(2)
    c = call("edit_file", path="pkg/mod0.py", old="a", new="b")
    st.add_entry("model", ChatMessage(role="assistant", tool_calls=[c]))
    read, mod = set(), {"prev/changed.py"}
    extract_file_ops(st.entries, read, mod)
    out = format_file_ops(read, mod)
    assert "<read-files>\npkg/mod1.py\n</read-files>" in out
    assert "pkg/mod0.py" in out.split("<modified-files>")[1] and "prev/changed.py" in out


def test_anchor_index_budget_and_content():
    st = _state(3)
    st.entries[2].msg.content += "\nTraceback ...\nValueError: bad value in astropy/io/fits/x.py"
    idx = build_anchor_index(st.entries, budget_chars=7000)
    assert "ValueError" in idx and "astropy/io/fits/x.py" in idx
    assert len(build_anchor_index(_state(200).entries, budget_chars=300)) < 800


def test_verbatim_user_only_real_input():
    st = _state(1)
    st.add_entry("notice", ChatMessage(role="user", content="[Harness] budget"))
    st.add_entry("incoming", ChatMessage(role="user", content="please keep API stable"))
    out = build_verbatim_user_section(st.entries, budget_chars=100, max_chars=50)
    assert "please keep API stable" in out and "[Harness]" not in out


def test_fallback_summary_bounded():
    st = _state(3000, tool_chars=50)
    s = fallback_summary(st.entries[1:], "old summary", max_chars=5000)
    assert len(s) < 6000 and "actions omitted" in s and "old summary" in s
