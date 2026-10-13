"""docs/60 §11 D13: a run halted by the provider account is paused, and resumes after a top-up.

Uses the scripted team of test_team_runtime; the provider answers with quota exhaustion first,
then answers normally once the account is topped up."""

import asyncio

from test_team_runtime import FnLlm, events, fin, go, setup

from sciteam.experiment.quota import is_paused
from sciteam.providers.llm.types import QuotaExhausted
from sciteam.runtime.team import reset


async def test_quota_halt_pauses_and_resumes_after_topup(tmp_path):
    run = setup(tmp_path, tasks=[{"key": "a", "by": "owner", "title": "A", "assignee": "dev1"}])

    # the account is empty: the first activation fails with quota exhaustion
    assert await go(run, {"dev1": FnLlm([QuotaExhausted("Insufficient Balance")])}) == "breaker:provider"
    assert is_paused(run)

    # top-up: clear the breaker and continue; the failed activation is retried and succeeds
    reset(run, breaker=True)
    end = await go(run, {"dev1": FnLlm([fin("completed", "done")])})
    assert end != "breaker:provider"
    assert not is_paused(run)

    finished = [e.payload for e in events(run, "activation_finished")]
    outcomes = [(p["outcome"], p.get("reason")) for p in finished]
    assert outcomes[0] == ("error", "provider_quota")
    assert any(o == "completed" for o, _ in outcomes[1:])


async def test_activation_cancelled_by_the_pause_is_retried_after_topup(tmp_path):
    # dev2 is mid-activation when dev1 hits the quota: the pause cancels it. After the top-up
    # it must still get to do its work, not stay idle with its delivered items consumed.
    run = setup(
        tmp_path,
        tasks=[
            {"key": "a", "by": "owner", "title": "A", "assignee": "dev1"},
            {"key": "b", "by": "owner", "title": "B", "assignee": "dev2"},
        ],
    )

    async def hang(messages):
        await asyncio.Event().wait()

    assert (
        await go(run, {"dev1": FnLlm([QuotaExhausted("Insufficient Balance")]), "dev2": FnLlm([hang])})
        == "breaker:provider"
    )
    assert is_paused(run)

    reset(run, breaker=True)
    await go(run, {"dev1": FnLlm([fin("completed", "a done")]), "dev2": FnLlm([fin("completed", "b done")])})
    done_by = [
        e.stream for e in events(run, "activation_finished") if e.payload.get("outcome") == "completed"
    ]
    assert any("dev2" in s for s in done_by), done_by
