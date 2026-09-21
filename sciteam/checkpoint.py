"""Resuming a campaign without re-deciding what was already decided.

A process killed halfway through leaves an awkward question: which of its
work still stands? The tempting answer is "re-run everything and see" —
which is wrong here for an institutional reason, not merely an economic
one. A mission whose outcome was accepted produced an *institutional
effect*: an artifact counted, an audit was reported, a close was recorded.
Running it again would either duplicate that effect or silently replace it,
and in both cases the ledger would no longer describe what actually
happened.

So resume is defined as: **replay the record to rebuild in-memory state,
never to redo the acts**. Concretely, for each mission the ledger already
records as decided, the resumed campaign

* restores the outcome from the ledger rather than calling the runner,
* feeds it to ``planner.observe()`` so the plan DAG reaches the same shape,
* re-charges the budget clock for it, so a resumed run cannot spend the
  budget twice,

and only then continues from the first undecided mission. The runner is
never invoked for a decided mission; :mod:`tests.test_checkpoint` asserts
exactly that, because it is the property that makes resume safe rather than
merely convenient.

Resume extends the *same* hash chain — :class:`~sciteam.event_log.EventLog`
recovers the head when it reopens a file — so a crash and restart leave one
continuous, verifiable record instead of two partial ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sciteam.event_log import EventLog, InstitutionEvent, read_events
from sciteam.mission_contracts import MissionOutcome

#: Ledger kinds that carry a mission-level decision, and whether the
#: mission's result counted.
_DECISION_KINDS = {"artifact_accepted": True, "artifact_rejected": False}


@dataclass(slots=True)
class ResumeState:
    """What a previous, interrupted run already decided."""

    run_id: str = ""
    outcomes: list[MissionOutcome] = field(default_factory=list)
    tokens_charged: int = 0
    finished: bool = False

    @property
    def decided_mission_ids(self) -> list[str]:
        return [o.mission_id for o in self.outcomes]

    def __bool__(self) -> bool:
        return bool(self.outcomes)


def read_resume_state(source: EventLog | Path | str) -> ResumeState:
    """Reconstruct prior decisions from a ledger.

    Only entries that recorded a mission decision are used. Observations,
    provenance and plugin events are ignored here by design: they describe
    the run, they do not constitute it.
    """
    if isinstance(source, EventLog):
        events: list[InstitutionEvent] = source.events()
    else:
        events = read_events(source)

    state = ResumeState()
    seen: set[str] = set()
    for event in events:
        if not state.run_id:
            state.run_id = event.run_id
        if event.kind == "run_finished":
            state.finished = True
            continue
        if event.kind == "llm_call":
            usage = event.payload.get("usage") or {}
            state.tokens_charged += int(usage.get("total_tokens") or 0)
            continue
        if event.kind not in _DECISION_KINDS:
            continue
        raw = event.payload.get("outcome")
        if not isinstance(raw, dict) or "mission_id" not in raw:
            continue
        outcome = MissionOutcome.from_dict(raw)
        # A mission can be reopened and decided again; the later decision
        # is the one in force, exactly as `replay` treats effective
        # artifacts. Keep one entry per mission, in first-decision order.
        if outcome.mission_id in seen:
            for index, existing in enumerate(state.outcomes):
                if existing.mission_id == outcome.mission_id:
                    state.outcomes[index] = outcome
                    break
        else:
            seen.add(outcome.mission_id)
            state.outcomes.append(outcome)
    return state


def record_mission_decision(
    ledger: EventLog,
    outcome: MissionOutcome,
    *,
    seat: str | None = None,
) -> InstitutionEvent:
    """Write the counts-as decision for one finished mission.

    The full outcome goes in the payload because the ledger is what a
    resumed run reads: a record that cannot reconstruct the decision it
    describes is a log, not a ledger.
    """
    return ledger.append(
        "artifact_accepted" if outcome.succeeded else "artifact_rejected",
        effect="accepted" if outcome.succeeded else "rejected",
        seat=seat,
        authority="may_emit_artifact",
        payload={
            "subject": outcome.mission_id,
            "status": outcome.status,
            "reason": outcome.detail or (outcome.contract_errors or [""])[0] or None,
            "outcome": outcome.to_dict(),
        },
    )


def describe(state: ResumeState) -> dict[str, Any]:
    """A short, printable summary for operators deciding whether to resume."""
    return {
        "run_id": state.run_id,
        "finished": state.finished,
        "decided_missions": state.decided_mission_ids,
        "succeeded": sum(1 for o in state.outcomes if o.succeeded),
        "tokens_charged": state.tokens_charged,
    }
