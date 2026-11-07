"""Budget checkpoints: a reached limit pauses for a grant and continues with the same context."""

import asyncio

import pytest
import yaml

from sciteam.harness import HarnessConfig
from sciteam.runtime.team import budget_view, create_run, decide_budget, request_stop, set_budget
from tests.scenarios.test_harness_loop import Crash, env, fin, make, tc  # noqa: F401
from tests.scenarios.test_team_runtime import (
    TEST_MODEL,
    FnLlm,
    TeamRun,
    events,
    go,
    human_says,
    write_configs,
)


def _pause_cfg(steps=2):
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.budget.max_steps = steps
    cfg.budget.on_exhausted = "pause"
    return cfg


async def test_grant_continues_same_activation(env):  # noqa: F811
    seen = []

    async def grant(pause):
        seen.append(pause)
        return {"actor": "owner", "add": {}}

    steps = [tc("bash", command=f"echo {i}") for i in range(3)] + [fin()]
    h = make(env, steps, cfg=_pause_cfg(), await_budget_grant=grant)
    r = await h.run("long task")
    assert r.outcome == "completed" and r.steps == 4
    assert seen[0]["binding"] == "steps" and seen[0]["limits"]["steps"] == 2
    types = [e.type for e in env["store"].read(h.stream)]
    assert types.count("budget_paused") == 1 and types.count("budget_granted") == 1
    assert "budget_exhausted" not in h.state.notices_sent
    assert any("echo 0" in str(m) for m in h.llm.requests[-1][0])


async def test_decline_ends_with_closing_call(env):  # noqa: F811
    async def decline(pause):
        return None

    steps = [tc("bash", command="echo 1"), tc("bash", command="echo 2"),
             fin(outcome="handoff", summary="stopped by owner")]
    h = make(env, steps, cfg=_pause_cfg(), await_budget_grant=decline)
    r = await h.run("task")
    assert r.outcome == "budget_exhausted" and r.summary == "stopped by owner"
    assert any(e.type == "budget_declined" for e in env["store"].read(h.stream))


async def test_pending_pause_survives_resume(env):  # noqa: F811
    async def crash(pause):
        raise Crash()

    steps = [tc("bash", command="echo 1"), tc("bash", command="echo 2")]
    h1 = make(env, steps, cfg=_pause_cfg(), await_budget_grant=crash)
    with pytest.raises(Crash):
        await h1.run("task")
    first = [e for e in env["store"].read(h1.stream) if e.type == "budget_paused"]
    assert len(first) == 1
    seen = []

    async def grant(pause):
        seen.append(pause["pause_id"])
        return {"actor": "owner", "add": {"steps": 5}}

    h2 = make(env, [fin()], cfg=_pause_cfg(), await_budget_grant=grant)
    r = await h2.resume(h1.stream)
    assert r.outcome == "completed" and seen == [first[0].payload["pause_id"]]
    assert h2.state.budget_extra == {"steps": 5}
    assert sum(e.type == "budget_paused" for e in env["store"].read(h1.stream)) == 1


async def test_token_checkpoint(env):  # noqa: F811
    cfg = HarnessConfig(name="t", model="scripted")
    cfg.budget.tokens = 50
    cfg.budget.on_exhausted = "pause"
    bindings = []

    async def grant(pause):
        bindings.append(pause["binding"])
        return {"actor": "owner", "add": {"tokens": 10_000_000}}

    h = make(env, [tc("bash", command="true"), tc("bash", command="true"), fin()], cfg=cfg,
             await_budget_grant=grant)
    r = await h.run("task")
    assert r.outcome == "completed" and bindings == ["tokens"]
    assert h.state.budget_extra["tokens"] == 10_000_000


# ---- team runs --------------------------------------------------------------------------

def _team(tmp_path, runtime):
    members = [{"id": "lead", "role": "lead", "team": "core", "harness": "harness/test.yaml"},
               {"id": "owner", "kind": "human", "role": "owner"}]
    cfg = write_configs(tmp_path, runtime=runtime, members=members)
    harness = cfg / "harness/test.yaml"
    h = yaml.safe_load(harness.read_text())
    h["budget"] = {"max_steps": 2, "on_exhausted": "pause"}
    harness.write_text(yaml.safe_dump(h))
    run = tmp_path / "run"
    create_run(run, team="teams/t.yaml", configs_dir=cfg, goal="test goal")
    human_says(run, "@lead", "please do the work")
    return run


def _payloads(run, t):
    return [e.payload for e in events(run, t)]


async def _owner_until(run, pred, act, timeout=15):
    for _ in range(int(timeout / 0.05)):
        await asyncio.sleep(0.05)
        tr = TeamRun(run, models={"scripted": TEST_MODEL})
        try:
            view = budget_view(tr.store)
        finally:
            tr.close()
        if pred(view):
            act(view)
            return


async def _stop_when(run, pred, timeout=15):
    for _ in range(int(timeout / 0.05)):
        await asyncio.sleep(0.05)
        if pred():
            break
    request_stop(run)


def _completed(run):
    return any(p.get("outcome") == "completed"
               for p in _payloads(run, "activation_finished"))


LONG = [tc("bash", command="echo 1"), tc("bash", command="echo 2"),
        tc("bash", command="echo 3"), fin()]


async def test_team_always_auto_continues(tmp_path):
    run = _team(tmp_path, {"budget": {"auto_continue": "always"}})
    await go(run, {"lead": FnLlm(LONG)})
    decided = _payloads(run, "budget_decided")
    assert decided and all(d["actor"] == "auto_policy" and d["decision"] == "grant"
                           for d in decided)
    assert _completed(run)


async def test_team_batch_ask_has_nobody_to_ask(tmp_path):
    run = _team(tmp_path, {})
    steps = [tc("bash", command="echo 1"), tc("bash", command="echo 2"),
             fin(outcome="handoff", summary="closing")]
    await go(run, {"lead": FnLlm(steps, loop=fin())})
    assert not _payloads(run, "budget_approval_requested")
    assert any(p.get("outcome") == "budget_exhausted"
               for p in _payloads(run, "activation_finished"))


async def test_team_owner_grant_unblocks_waiting_activation(tmp_path):
    run = _team(tmp_path, {"on_quiescent": "wait"})

    def grant(view):
        assert view["pending"][0]["scope"] == "activation"
        decide_budget(run, view["pending"][0]["pause_id"], "grant")

    await asyncio.gather(
        go(run, {"lead": FnLlm(LONG)}),
        _owner_until(run, lambda v: v["pending"], grant),
        _stop_when(run, lambda: _completed(run)))
    assert [d["actor"] for d in _payloads(run, "budget_decided")] == ["owner"]
    assert _completed(run)


async def test_switching_to_always_releases_the_waiting_pause(tmp_path):
    run = _team(tmp_path, {"on_quiescent": "wait"})
    await asyncio.gather(
        go(run, {"lead": FnLlm(LONG)}),
        _owner_until(run, lambda v: v["pending"],
                     lambda v: set_budget(run, auto_continue="always")),
        _stop_when(run, lambda: _completed(run)))
    assert [d["actor"] for d in _payloads(run, "budget_decided")] == ["auto_policy"]
    assert _completed(run)


async def test_handoff_limit_asks_and_auto_policy_counts(tmp_path):
    run = _team(tmp_path, {"rate": {"max_consecutive_handoffs": 1},
                           "budget": {"auto_continue": "up_to", "auto_continue_max": 1}})
    handoff = fin(outcome="handoff", summary="more to do")
    await go(run, {"lead": FnLlm([handoff, handoff, handoff, fin()])})
    reqs = _payloads(run, "budget_approval_requested")
    assert [r["scope"] for r in reqs][:1] == ["handoffs"]
    decided = _payloads(run, "budget_decided")
    assert [d["actor"] for d in decided] == ["auto_policy"]
    assert _payloads(run, "handoff_limit")


async def test_run_token_budget_pauses_until_owner_grants(tmp_path):
    run = _team(tmp_path, {"on_quiescent": "wait", "budget": {"tokens": 10}})
    steps = [tc("bash", command="echo 1"), fin()]

    def grant(view):
        assert view["pending"][0]["scope"] == "run"
        set_budget(run, auto_continue="always")
        decide_budget(run, view["pending"][0]["pause_id"], "grant", add={"tokens": 1e9})

    await asyncio.gather(
        go(run, {"lead": FnLlm(steps, loop=fin())}),
        _owner_until(run, lambda v: v["pending"], grant),
        _stop_when(run, lambda: bool(_payloads(run, "budget_decided")) and _completed(run)))
    assert _payloads(run, "breaker_tripped")[0]["kind"] == "budget"
    assert _payloads(run, "budget_decided")[0]["decision"] == "grant"
