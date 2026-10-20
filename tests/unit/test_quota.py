"""docs/60 §11 D13: provider-quota pauses, probes and the wait budget (no model calls)."""

from pathlib import Path

import pytest

from sciteam.experiment.quota import (
    PAUSE_REASON,
    await_provider,
    is_paused,
    last_stop,
    models_for_spec,
    probe,
)
from sciteam.experiment.spec import load_spec
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import EventStore
from sciteam.providers.llm import load_models
from sciteam.providers.llm.types import (
    AssistantTurn,
    AuthError,
    ModelInfo,
    QuotaExhausted,
    RateLimited,
)

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def _info(name: str = "m") -> ModelInfo:
    return ModelInfo(
        name=name,
        provider="openai_compat",
        base_url="http://x",
        api_key_env="K",
        wire_model=name,
        context_window=1000,
        max_output_tokens=100,
        pricing_usd_per_mtok={},
    )


def _run_dir(tmp_path: Path, stops: list[str]) -> Path:
    rd = tmp_path / "run"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text("{}")
    store = EventStore(rd / "events.db")
    for reason in stops:
        store.append("run", "run_stopped", {"reason": reason}, actor="system")
    store.close()
    return rd


def test_paused_only_when_last_stop_is_the_provider_breaker(tmp_path):
    assert is_paused(_run_dir(tmp_path / "a", [PAUSE_REASON]))
    assert not is_paused(_run_dir(tmp_path / "b", ["breaker:budget"]))
    # a run resumed after a pause and finished is no longer paused
    rd = _run_dir(tmp_path / "c", [PAUSE_REASON, "goal_met"])
    assert last_stop(rd) == "goal_met"
    assert not is_paused(rd)


def test_not_paused_without_a_run_directory(tmp_path):
    assert not is_paused(tmp_path / "missing")


class _Llm:
    def __init__(self, exc: BaseException | None):
        self.exc = exc

    async def complete(self, messages, tools, *, params):
        if self.exc is not None:
            raise self.exc
        return AssistantTurn(content="pong")


@pytest.mark.parametrize(
    "exc,ready",
    [
        (None, True),
        (QuotaExhausted("Insufficient Balance"), False),
        (AuthError("bad key"), False),
        (RateLimited("slow down"), False),
        (ValueError("connection reset"), False),
    ],
)
def test_probe_reports_ready_only_when_the_account_answers(exc, ready):
    ok, why = probe(_info(), make=lambda info: _Llm(exc))
    assert ok is ready
    assert why


def test_probe_names_the_quota_failure():
    ok, why = probe(_info(), make=lambda info: _Llm(QuotaExhausted("402 empty")))
    assert not ok and why.startswith("quota:")


def test_await_provider_waits_until_every_model_answers():
    answers = iter([(False, "quota"), (True, "ok")])  # m1 down on the first probe, then up
    seen_sleeps: list[float] = []
    calls: list[str] = []

    def probe_fn(info):
        calls.append(info.name)
        return next(answers) if info.name == "m1" else (True, "ok")

    models = [_info("m1"), _info("m2")]
    ready, waited = await_provider(
        models,
        poll_s=300,
        max_wait_s=3600,
        probe_fn=probe_fn,
        sleep_fn=seen_sleeps.append,
        log=lambda s: None,
    )
    assert ready and waited == 300
    assert seen_sleeps == [300]
    assert calls == ["m1", "m2", "m1", "m2"]


def test_await_provider_gives_up_when_the_budget_is_spent():
    seen: list[float] = []
    ready, waited = await_provider(
        [_info()],
        poll_s=600,
        max_wait_s=1000,
        probe_fn=lambda info: (False, "quota"),
        sleep_fn=seen.append,
        log=lambda s: None,
    )
    assert not ready
    assert waited == 1000 and sum(seen) == 1000
    assert seen == [600, 400]  # the last sleep is cut to the remaining budget


def _supervised(monkeypatch, tmp_path: Path, probe_fn, launch_fn, **kw):
    """Run the supervisor with a fake launcher: no model is ever called."""
    from sciteam.experiment import runner

    launches: list[int] = []

    def fake_launch(configs_dir, spec_path, out, keys, parallel):
        launches.append(len(keys))
        launch_fn(out, keys)

    monkeypatch.setattr(runner, "_launch", fake_launch)
    msgs: list[str] = []
    results = runner.run_all(
        CONFIGS,
        "experiments/x-inst-confirm.yaml",
        tmp_path / "exp",
        poll_s=0.001,
        probe_fn=probe_fn,
        log=msgs.append,
        **kw,
    )
    return results, launches, msgs


def _write_result(out: Path, key) -> None:
    rd = key.dir(out)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "result.json").write_text("{}")


def test_supervisor_waits_for_topup_then_finishes_the_open_runs(monkeypatch, tmp_path):
    from sciteam.experiment.runner import RESULT, plan
    from sciteam.experiment.spec import load_spec

    spec, ts = load_spec(ConfigStore(CONFIGS), "experiments/x-inst-confirm.yaml")
    n_keys = len(plan(spec, ts))
    probes = {"n": 0}

    def probe_fn(info):  # not ready for two probes, then topped up
        probes["n"] += 1
        return (probes["n"] >= 3, "quota" if probes["n"] < 3 else "ok")

    rounds = {"n": 0}

    def launch(out, keys):
        rounds["n"] += 1
        if rounds["n"] == 2:  # round 1: every run paused (writes nothing)
            for k in keys:
                _write_result(out, k)

    results, launches, _ = _supervised(monkeypatch, tmp_path, probe_fn, launch, max_pause_s=60)
    assert launches == [n_keys, n_keys]
    assert len(results) == n_keys
    assert all((k.dir(tmp_path / "exp") / RESULT).exists() for k in plan(spec, ts))


def test_supervisor_does_not_launch_while_the_account_is_empty(monkeypatch, tmp_path):
    results, launches, msgs = _supervised(
        monkeypatch, tmp_path, lambda info: (False, "quota"), lambda out, keys: None, max_pause_s=0.01
    )
    assert launches == [] and results == []
    text = "\n".join(msgs)
    assert "provider not ready" in text and "stopped: provider not ready" in text


def test_supervisor_stops_after_three_launches_without_progress(monkeypatch, tmp_path):
    # the probe passes but every run keeps failing: do not loop forever
    results, launches, msgs = _supervised(
        monkeypatch, tmp_path, lambda info: (True, "ok"), lambda out, keys: None, max_pause_s=60
    )
    assert len(launches) == 3 and results == []
    assert any("three launches without progress" in m for m in msgs)


def test_models_for_spec_resolves_each_agents_model_through_its_overlay():
    spec, ts = load_spec(ConfigStore(CONFIGS), "experiments/x-inst-confirm.yaml")
    models = models_for_spec(CONFIGS, spec, ts)
    names = {m.name for m in models}
    assert names
    assert names <= set(load_models(CONFIGS / "models.yaml"))
    assert names == {"mimo-v2.6-flash"}  # every arm patches the harness model


def test_wall_clock_budget_excludes_paused_intervals_between_run_stopped_and_run_resumed(tmp_path):
    """D20 regression test: waiting 7 hours for a provider top-up between run_stopped and
    run_resumed must not count against the active wall_clock_s budget."""
    from datetime import UTC, datetime, timedelta

    from sciteam.comm.policy import CommPolicy
    from sciteam.runtime.config import RuntimeConfig
    from sciteam.runtime.scheduler import Scheduler

    class FakeClock:
        def __init__(self, t: datetime):
            self.t = t

        def now(self) -> datetime:
            return self.t

    t0 = datetime(2026, 10, 18, 23, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    store = EventStore(tmp_path / "events.db", clock=clock)
    store.append("run", "run_started", {"goal": "g"}, actor="system")

    # 10 minutes into the run, provider quota is exhausted and the run stops
    clock.t = t0 + timedelta(minutes=10)
    store.append("run", "breaker_tripped", {"kind": "provider"}, actor="system")
    store.append("run", "run_stopped", {"reason": "breaker:provider"}, actor="system")

    # 7 hours later (> 6h wall_clock_s budget), quota is topped up and breaker reset
    clock.t = t0 + timedelta(hours=7, minutes=10)
    store.append("run", "breaker_reset", {"kind": "provider"}, actor="human")
    store.append("run", "run_resumed", {"after": "breaker:provider"}, actor="human")

    cfg = ConfigStore(CONFIGS).load("runtime/x-evid.yaml", RuntimeConfig)
    cfg.budget.wall_clock_s = 21600  # 6 hours

    class DummyRunner:
        async def close(self) -> None:
            pass

    sched = Scheduler(
        store=store,
        policy=ConfigStore(CONFIGS).load("comm/x-evid.yaml", CommPolicy),
        runtime=cfg,
        runner=DummyRunner(),  # type: ignore[arg-type]
        clock=clock,
        goal="g",
    )
    sched.catch_up()
    assert sched.paused_s == 7 * 3600
    sched._check_breakers(clock.now())
    assert sched.paused is None  # only 10m of active wall time elapsed, not 7h10m!

    # Once active time actually exceeds 6h (another 5h51m of active execution), budget trips
    clock.t = clock.t + timedelta(hours=5, minutes=51)
    sched._check_breakers(clock.now())
    assert sched.paused == "budget"
    store.close()
