"""End-of-run health/cost/resilience summary — docs/28 §7.2 (E7).

A headless product's operator does not watch a live TUI; the artifact they
get at the end of a run is what tells them whether it is safe to trust. This
module composes three things this package already tracks separately —
institutional decisions (`sciteam.checkpoint`), attributed spend
(`sciteam.event_log.cost_report`), and provider resilience metrics
(`sciteam.resilience.ResilientRuntimePort.metrics`) — into one small,
JSON-serializable `RunSummary`, and reserves (but does not implement) an
`ObserverPort` for exporting it to an external backend later.

v1 explicitly does not ship OTel/Prometheus exporters (docs/28 §7.2); the
`NullObserver` default makes that a visible decision rather than a silent
gap, and a composition root can inject a real `ObserverPort` without
touching this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sciteam.checkpoint import read_resume_state
from sciteam.event_log import EventLog, cost_report


@runtime_checkable
class ObserverPort(Protocol):
    """Reserved export hook. No v1 implementation ships in this package;
    a composition root may inject one (e.g. an OTel exporter) later."""

    def observe_run_summary(self, summary: dict[str, Any]) -> None: ...


class NullObserver:
    """Default `ObserverPort`: does nothing, on purpose."""

    def observe_run_summary(self, summary: dict[str, Any]) -> None:
        return None


@dataclass(slots=True)
class RunSummary:
    run_id: str
    finished: bool
    missions_decided: int
    missions_succeeded: int
    missions_failed: int
    tokens_charged: int
    total_usd: float
    cost_by_seat: list[dict[str, Any]] = field(default_factory=list)
    resilience: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "finished": self.finished,
            "missions_decided": self.missions_decided,
            "missions_succeeded": self.missions_succeeded,
            "missions_failed": self.missions_failed,
            "tokens_charged": self.tokens_charged,
            "total_usd": round(self.total_usd, 6),
            "cost_by_seat": self.cost_by_seat,
            "resilience": self.resilience,
        }


def build_run_summary(
    ledger: EventLog | Path | str,
    *,
    resilient_runtime: Any = None,
) -> RunSummary:
    """Build the end-of-run summary from an institution ledger, plus a
    resilience snapshot when `resilient_runtime` exposes `.metrics()`
    (e.g. `sciteam.resilience.ResilientRuntimePort`).
    """
    resume_state = read_resume_state(ledger)
    lines = cost_report(ledger.events() if isinstance(ledger, EventLog) else ledger)
    total_usd = sum(line.usd for line in lines)
    metrics_fn = getattr(resilient_runtime, "metrics", None)
    resilience = metrics_fn() if callable(metrics_fn) else {}
    succeeded = sum(1 for o in resume_state.outcomes if o.succeeded)
    return RunSummary(
        run_id=resume_state.run_id,
        finished=resume_state.finished,
        missions_decided=len(resume_state.outcomes),
        missions_succeeded=succeeded,
        missions_failed=len(resume_state.outcomes) - succeeded,
        tokens_charged=resume_state.tokens_charged,
        total_usd=total_usd,
        cost_by_seat=[
            {
                "seat": line.seat,
                "model": line.model,
                "calls": line.calls,
                "prompt_tokens": line.prompt_tokens,
                "completion_tokens": line.completion_tokens,
                "usd": line.usd,
            }
            for line in lines
        ],
        resilience=dict(resilience),
    )


def write_run_summary(
    path: Path | str,
    summary: RunSummary,
    *,
    observer: ObserverPort | None = None,
) -> Path:
    """Write the summary as one indented JSON document and, if an
    `ObserverPort` was injected, also forward it there."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (observer or NullObserver()).observe_run_summary(summary.to_dict())
    return out
