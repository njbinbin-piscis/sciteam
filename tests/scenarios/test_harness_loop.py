import asyncio
from typing import Any

import pytest

from sciteam.harness import Harness, HarnessConfig
from sciteam.harness.tools.base import ToolResult
from sciteam.kernel import BlobStore, EventStore, FakeClock
from sciteam.providers.llm.fakes import TEST_MODEL, ScriptedLlm, call
from sciteam.providers.llm.types import AssistantTurn, Transient, Usage
from sciteam.providers.sandbox import LocalSandbox


def fin(outcome="completed", summary="done", **kw):
    return AssistantTurn(tool_calls=[call("finish", outcome=outcome, summary=summary, **kw)],
                         finish_reason="tool_calls")


def tc(name, **args):
    return AssistantTurn(tool_calls=[call(name, **args)], finish_reason="tool_calls")


def text(s):
    return AssistantTurn(content=s)


@pytest.fixture
async def env(tmp_path):
    sb = LocalSandbox(tmp_path / "work")
    await sb.start()
    store = EventStore(tmp_path / "events.db")
    blobs = BlobStore(tmp_path / "blobs")
    yield {"sb": sb, "store": store, "blobs": blobs, "tmp": tmp_path}
    await sb.close()
    store.close()


def make(env, steps, cfg: HarnessConfig | None = None, model=TEST_MODEL, **kw) -> Harness:
    cfg = cfg or HarnessConfig(name="t", model="scripted")
    llm = kw.pop("llm", None) or ScriptedLlm(steps, model_info=model)
    return Harness(cfg, model_info=model, llm=llm, sandbox=env["sb"], store=env["store"],
                   blobs=env["blobs"], system_prompt="You are a test agent.", **kw)


def _check_transcript_valid(requests):
    """Every tool message must answer a tool call of the nearest preceding assistant message."""
    for msgs, _, _ in requests:
        open_ids: set[str] = set()
        for m in msgs:
            if m.role == "assistant":
                open_ids = {c.id for c in m.tool_calls}
            elif m.role == "tool":
                assert m.tool_call_id in open_ids, "orphan tool result"
                open_ids.discard(m.tool_call_id)


async def test_basic_completion(env):
    h = make(env, [tc("bash", command="echo hi > f.txt && cat f.txt"), fin(artifacts=["f.txt"])])
    r = await h.run("write f.txt")
    assert r.outcome == "completed" and r.artifacts == ["f.txt"] and r.steps == 2
    assert r.totals.prompt_tokens > 0 and r.totals.cost_usd > 0
    tool_msgs = [e for e in h.state.entries if e.kind == "tool"]
    assert "hi" in tool_msgs[0].msg.content and "[exit code: 0]" in tool_msgs[0].msg.content
    assert env["store"].verify(h.stream).ok
    _check_transcript_valid(h.llm.requests)


async def test_no_finish_is_error_not_silent(env):
    h = make(env, [text("I think it's done"), text("really done"), text("done!")])
    r = await h.run("task")
    assert r.outcome == "error" and r.reason == "no_finish"
    notices = [e for e in h.state.entries if e.kind == "notice"]
    assert len(notices) == 2


async def test_text_then_recover(env):
    h = make(env, [text("thinking out loud"), fin()])
    assert (await h.run("task")).outcome == "completed"


async def test_invalid_args_and_unknown_tool_reported(env):
    bad = AssistantTurn(tool_calls=[call("bash"), call("nosuch", x=1)], finish_reason="tool_calls")
    bad.tool_calls[0].arguments = None
    bad.tool_calls[0].arguments_raw = '{"command": '
    h = make(env, [bad, fin()])
    r = await h.run("task")
    assert r.outcome == "completed"
    outs = [e.msg.content for e in h.state.entries if e.kind == "tool"]
    assert "not a valid JSON" in outs[0] and "unknown tool" in outs[1]


async def test_finish_check_rejects_then_accepts(env):
    seen = []

    async def check(args: dict[str, Any], ctx) -> str | None:
        seen.append(args["summary"])
        return "patch is empty" if len(seen) == 1 else None

    h = make(env, [fin(summary="first"), fin(summary="second")], finish_check=check)
    r = await h.run("task")
    assert r.outcome == "completed" and r.summary == "second"
    assert any("patch is empty" in e.msg.content for e in h.state.entries if e.kind == "tool")


async def test_finish_with_failed_sibling_call_rejected(env):
    both = AssistantTurn(tool_calls=[call("read_file", path="missing.txt"),
                                     call("finish", outcome="completed", summary="x")],
                         finish_reason="tool_calls")
    h = make(env, [both, fin()])
    r = await h.run("task")
    assert r.outcome == "completed" and r.steps == 2


async def test_step_budget_notices_and_closing_call(env):
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.budget.max_steps = 10
    steps = [tc("bash", command=f"echo {i}") for i in range(10)]
    steps.append(fin(outcome="handoff", summary="ran out"))
    h = make(env, steps, cfg=cfg)
    r = await h.run("task")
    assert r.outcome == "budget_exhausted" and r.reason == "steps" and r.summary == "ran out"
    keys = h.state.notices_sent
    assert {"budget_warn", "budget_decide", "budget_exhausted"} <= keys
    last_tools = h.llm.requests[-1][1]
    assert [t.name for t in last_tools] == ["finish"]


async def test_closing_call_text_reply_becomes_summary(env):
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.budget.max_steps = 1
    h = make(env, [tc("bash", command="true"), text("handoff: nothing left")], cfg=cfg)
    r = await h.run("task")
    assert r.outcome == "budget_exhausted" and r.summary == "handoff: nothing left"


async def test_cost_budget(env):
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.budget.cost_usd = 0.5
    pricey = AssistantTurn(tool_calls=[call("bash", command="true")], finish_reason="tool_calls",
                           usage=Usage(prompt_tokens=300_000, cache_miss_tokens=300_000,
                                       completion_tokens=10))
    h = make(env, [pricey, pricey, fin(outcome="handoff")], cfg=cfg)
    r = await h.run("task")
    assert r.outcome == "budget_exhausted" and r.reason == "cost"


async def test_provider_retry_then_success(env, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    h = make(env, [Transient("503"), Transient("reset"), fin()])
    r = await h.run("task")
    assert r.outcome == "completed"
    rec = [e for e in env["store"].read(h.stream) if e.type == "recovery"]
    assert len(rec) == 2


async def test_provider_down_is_error(env, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.recovery.provider_retries = 2
    h = make(env, [Transient("x")] * 3, cfg=cfg)
    r = await h.run("task")
    assert r.outcome == "error" and r.reason == "provider_unavailable"


async def _nosleep(*a, **k):
    return None


class SlowRead:
    readonly = True
    active = 0
    peak = 0

    def __init__(self):
        from sciteam.providers.llm.types import ToolSpec
        self.spec = ToolSpec(name="slow_read", description="", parameters={
            "type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"]})

    async def run(self, args, ctx):
        SlowRead.active += 1
        SlowRead.peak = max(SlowRead.peak, SlowRead.active)
        await asyncio.sleep(0.05)
        SlowRead.active -= 1
        return ToolResult(content=f"k={args['k']}")


async def test_parallel_readonly_bounded_and_ordered(env):
    cfg = HarnessConfig(name="t", model="scripted", tools=["bash", "slow_read", "finish"])
    cfg.tool_output.parallel_readonly = 3
    calls = [call("slow_read", k=i) for i in range(8)]
    calls.insert(4, call("bash", command="echo side"))
    turn = AssistantTurn(tool_calls=calls, finish_reason="tool_calls")
    h = make(env, [turn, fin()], cfg=cfg, extra_tools={"slow_read": SlowRead()})
    await h.run("task")
    assert 1 < SlowRead.peak <= 3
    outs = [e.msg.content for e in h.state.entries if e.kind == "tool"]
    assert outs[:4] == [f"k={i}" for i in range(4)] and "side" in outs[4]
    assert outs[5:9] == [f"k={i}" for i in range(4, 8)]


async def test_loop_detection(env):
    steps = [tc("bash", command="echo same") for _ in range(6)] + [fin()]
    h = make(env, steps)
    await h.run("task")
    assert "loop_1" in h.state.notices_sent and "loop_2" in h.state.notices_sent


async def test_large_output_truncated_and_pageable(env):
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.tool_output.max_chars = 2000

    def page(msgs, tools):
        note = msgs[-1].content
        ref = note.split("ref='")[1].split("'")[0]
        return tc("read_output", ref=ref, offset=4990, limit=5)

    h = make(env, [tc("bash", command="seq 1 5000"), page, fin()], cfg=cfg)
    await h.run("task")
    outs = [e.msg.content for e in h.state.entries if e.kind == "tool"]
    assert len(outs[0]) < 2600 and "characters omitted" in outs[0]
    assert outs[0].rstrip().endswith("]")
    assert "4991\n4992" in outs[1]


SMALL = TEST_MODEL.model_copy(update={"context_window": 40_000})


def _compaction_cfg():
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.compaction.trigger_fraction = 0.5  # 20K tokens
    cfg.compaction.tail_min = 2_000
    cfg.compaction.tail_max = 4_000
    return cfg


def _summary_turn(msgs, tools):
    assert not tools and msgs[0].role == "system"
    prompt = msgs[1].content
    kind = "UPDATE" if "<previous-summary>" in prompt else "FIRST"
    return AssistantTurn(content=f"## Goal\nsummary-{kind}\n## Next Steps\n1. continue")


async def test_compaction_long_session_and_recall(env):
    marker = "NEEDLE-4711"
    steps = [tc("bash", command=f"echo {marker} src/pkg/mod.py; python3 -c \"print('z'*6000)\"")]
    steps += [tc("bash", command=f"python3 -c \"print('{i}'*6000)\"") for i in range(1, 50)]
    steps.append(tc("recall", query=marker))
    steps.append(fin())
    summarizer = ScriptedLlm([_summary_turn] * 20, model_info=SMALL)
    h = make(env, steps, cfg=_compaction_cfg(), model=SMALL, summarizer=summarizer)
    r = await h.run("long task")
    assert r.outcome == "completed", r
    assert r.compactions >= 2
    recs = h.state.compactions
    assert recs[0].method == "llm" and "summary-FIRST" in recs[0].summary
    assert "summary-UPDATE" in recs[-1].summary
    assert all(rec.tokens_after < rec.tokens_before for rec in recs)
    assert "## Anchor Index" in recs[-1].summary and "## Context Recovery" in recs[-1].summary
    # the request after compaction carries the task, the summary, and a valid tail
    _check_transcript_valid(h.llm.requests)
    for msgs, _, _ in h.llm.requests:
        assert msgs[1].content == "long task"
    recall_out = [e for e in h.state.entries if e.kind == "tool"][-2].msg.content
    assert marker in recall_out and "entry 1" in recall_out
    assert r.totals.summary_calls == len(summarizer.requests)


async def test_summarizer_failure_uses_fallback_with_cooldown(env):
    from sciteam.providers.llm.types import BadRequest

    steps = [tc("bash", command=f"python3 -c \"print('{i % 10}'*6000)\"") for i in range(30)]
    steps.append(fin())
    clock = FakeClock()
    summarizer = ScriptedLlm([BadRequest("nope")] * 20, model_info=SMALL)
    h = make(env, steps, cfg=_compaction_cfg(), model=SMALL, summarizer=summarizer, clock=clock)
    r = await h.run("task")
    assert r.outcome == "completed"
    methods = [c.method for c in h.state.compactions]
    assert methods and set(methods) == {"fallback"}
    # clock never advances, so only the first compaction attempts the summarizer (then cooldown)
    assert len(summarizer.requests) == 1
    assert "mechanical log" in h.state.compactions[0].summary


class Crash(BaseException):
    pass


async def test_crash_resume(env):
    steps = [tc("bash", command="echo one >> log.txt"), tc("bash", command="echo two >> log.txt"),
             Crash()]
    h1 = make(env, steps)
    with pytest.raises(Crash):
        await h1.run("task")
    stream = h1.stream
    assert env["store"].read(stream)[-1].type != "activation_finished"
    h2 = make(env, [tc("bash", command="cat log.txt"), fin()])
    r = await h2.resume(stream)
    assert r.outcome == "completed" and r.steps == 4  # 2 before the crash + 2 after resume
    out = [e.msg.content for e in h2.state.entries if e.kind == "tool"][-2]
    assert "one\ntwo" in out
    assert env["store"].verify(stream).ok
    _check_transcript_valid(h2.llm.requests)


async def test_resume_with_inflight_side_effect(env):
    class Boom(BaseException):
        pass

    h1 = make(env, [tc("bash", command="echo x")])
    orig = h1._handle_calls

    async def crash_mid(calls):
        h1._emit("tool_started", {"call_id": calls[0].id, "name": calls[0].name})
        raise Boom()

    h1._handle_calls = crash_mid
    with pytest.raises(Boom):
        await h1.run("task")
    assert orig
    h2 = make(env, [fin()])
    r = await h2.resume(h1.stream)
    assert r.outcome == "completed"
    unknown = [e for e in h2.state.entries if e.kind == "tool"][0].msg.content
    assert "result unknown" in unknown
    _check_transcript_valid(h2.llm.requests)


async def test_whole_activation_replay_is_consistent(tmp_path):
    """H7: a recorded activation replays offline with identical requests and event sequence."""
    from sciteam.providers.llm.fakes import RecordingLlm, ReplayLlm

    script = [tc("bash", command="printf 'a\\nb\\n' > f.txt && wc -l f.txt"),
              tc("edit_file", path="f.txt", old="b", new="c"),
              tc("read_file", path="f.txt"), text("forgot to finish"), fin(artifacts=["f.txt"])]
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.budget.status_every_steps = 2

    async def one(root, llm):
        sb = LocalSandbox(root / "work")
        await sb.start()
        store, blobs = EventStore(root / "events.db"), BlobStore(root / "blobs")
        h = Harness(cfg, model_info=TEST_MODEL, llm=llm(blobs), sandbox=sb, store=store,
                    blobs=blobs, system_prompt="You are a test agent.", clock=FakeClock())
        r = await h.run("edit f.txt", stream="replay/one")
        def untimed(x):
            if isinstance(x, dict):
                return {k: untimed(v) for k, v in x.items() if k not in ("duration_s", "wall_s")}
            return x

        evs = [(e.type, untimed(e.payload)) for e in store.read("replay/one")]
        await sb.close()
        store.close()
        return r, evs, h.llm

    index: list[dict] = []
    shared = BlobStore(tmp_path / "rec")
    r1, ev1, _ = await one(tmp_path / "a",
                           lambda b: RecordingLlm(ScriptedLlm(script, model_info=TEST_MODEL),
                                                  shared, index))
    r2, ev2, rep = await one(tmp_path / "b", lambda b: ReplayLlm(shared, index))
    assert r1.outcome == r2.outcome == "completed" and not rep.index
    assert [t for t, _ in ev1] == [t for t, _ in ev2]
    strip = {"activation_started", "activation_finished"}
    assert [e for e in ev1 if e[0] not in strip] == [e for e in ev2 if e[0] not in strip]


def _review_cfg(n=1, **budget):
    cfg = HarnessConfig(name="t", model="scripted", finish_reviews=n)
    for k, v in budget.items():
        setattr(cfg.budget, k, v)
    return cfg


async def test_finish_review_once_then_accept(env):
    h = make(env, [fin(), tc("bash", command="echo check"), fin()], cfg=_review_cfg())
    r = await h.run("write the file named EXACT.txt")
    assert r.outcome == "completed" and r.steps == 3 and h.state.finish_reviews == 1
    review = [e.msg.content for e in h.state.entries if e.kind == "tool"][0]
    assert "Finish review" in review and "EXACT.txt" in review
    _check_transcript_valid(h.llm.requests)


async def test_finish_review_off_by_default_and_skips_non_completed(env):
    assert (await make(env, [fin()]).run("t")).steps == 1
    r = await make(env, [fin(outcome="blocked")], cfg=_review_cfg()).run("t")
    assert r.outcome == "blocked" and r.steps == 1


async def test_finish_review_skipped_near_budget(env):
    h = make(env, [tc("bash", command="true")] * 9 + [fin()], cfg=_review_cfg(max_steps=11))
    r = await h.run("t")
    assert r.outcome == "completed" and h.state.finish_reviews == 0


async def test_finish_review_count_survives_resume(env):
    h1 = make(env, [fin(), Crash()], cfg=_review_cfg())
    with pytest.raises(Crash):
        await h1.run("t")
    h2 = make(env, [fin()], cfg=_review_cfg())
    r = await h2.resume(h1.stream)
    assert r.outcome == "completed" and h2.state.finish_reviews == 1


async def test_quota_exhausted_is_fatal_without_retries(env):
    from sciteam.providers.llm.types import QuotaExhausted

    h = make(env, [tc("bash", command="true"), QuotaExhausted("Insufficient Balance", status=402)])
    r = await h.run("t")
    assert r.outcome == "error" and r.reason == "provider_quota" and r.steps == 1
    assert not [e for e in env["store"].read(h.stream) if e.type == "recovery"]
