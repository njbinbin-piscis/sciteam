"""Provider quota pauses (docs/60-experiment.md §11 D13).

A team run halted by the provider account (balance or quota exhausted) is paused, not finished:
the runner writes no `result.json` for it, and the next launch resets the provider breaker and
resumes the run where it stopped (the scheduler re-drives interrupted activations and retries the
failed ones). A budget breaker is not a pause: it ends the run as before (docs/60 §3).

Before every launch the runner probes each model the experiment uses, so no run starts against an
empty account and burns its first activations for nothing."""

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import EventStore
from sciteam.providers.llm import load_models, make_provider
from sciteam.providers.llm.types import (
    AuthError,
    CallParams,
    ChatMessage,
    LlmError,
    ModelInfo,
    QuotaExhausted,
)

PAUSE_REASON = "breaker:provider"
PAUSED_MARK = "paused.json"  # human-readable marker in a paused run directory

ProbeFn = Callable[[ModelInfo], tuple[bool, str]]


def last_stop(run_dir: Path) -> str | None:
    store = EventStore(run_dir / "events.db")
    try:
        stops = [e.payload.get("reason") for e in store.read_global(types={"run_stopped"})]
    finally:
        store.close()
    return stops[-1] if stops else None


def is_paused(run_dir: Path) -> bool:
    """True when the run was halted by the provider account and has not finished since."""
    if not (run_dir / "run.json").exists() or not (run_dir / "events.db").exists():
        return False
    return last_stop(run_dir) == PAUSE_REASON


def probe(info: ModelInfo, make: Callable[[ModelInfo], object] = make_provider) -> tuple[bool, str]:
    """One minimal call. Quota exhaustion and auth failure mean the account cannot run now;
    any other provider error means the probe failed for now. All three are 'not ready'."""

    async def call() -> None:
        llm = make(info)
        await llm.complete(
            [ChatMessage(role="user", content="ping")],
            [],
            params=CallParams(max_output_tokens=1, timeout_s=60),
        )

    try:
        asyncio.run(call())
        return True, "ok"
    except QuotaExhausted as e:
        return False, f"quota: {str(e)[:200]}"
    except AuthError as e:
        return False, f"auth: {str(e)[:200]}"
    except LlmError as e:
        return False, f"{e.category}: {str(e)[:200]}"
    except Exception as e:  # network and client errors: retry later, do not crash the supervisor
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def models_for_spec(configs_dir: Path, spec, ts) -> list[ModelInfo]:
    """Every model an arm's agents use, resolved through the arm's overlay (not the task
    overlay: task patches do not change harnesses in the current task sets)."""
    from sciteam.experiment.spec import render, task_vars
    from sciteam.harness.config import HarnessConfig
    from sciteam.runtime.config import TeamFile

    models = load_models(configs_dir / "models.yaml")
    names: set[str] = set()
    tv = task_vars(ts, ts.tasks[0])
    for arm in spec.arms:
        overlays = [configs_dir / arm.overlay] if arm.overlay else []
        cs = ConfigStore(configs_dir, overlays)
        tf = cs.load(render(arm.team, tv), TeamFile)
        for m in tf.members:
            if m.kind == "agent" and m.harness:
                names.add(cs.load(m.harness, HarnessConfig).model)
    return [models[n] for n in sorted(names)]


def await_provider(
    models: Sequence[ModelInfo],
    *,
    poll_s: float,
    max_wait_s: float,
    probe_fn: ProbeFn = probe,
    sleep_fn: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> tuple[bool, float]:
    """Probe until every model answers or the wait budget is spent. Returns (ready, seconds
    waited). Probing happens before the first wait too, so an empty account is never launched."""
    waited = 0.0
    while True:
        bad = []
        for info in models:
            ok, why = probe_fn(info)
            if not ok:
                bad.append(f"{info.name} {why}")
        if not bad:
            return True, waited
        log(f"provider not ready after {waited / 3600:.2f} h: {'; '.join(bad)}")
        if waited >= max_wait_s:
            return False, waited
        step = min(poll_s, max_wait_s - waited)
        sleep_fn(step)
        waited += step
