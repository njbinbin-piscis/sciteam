"""Human budget gate: pause on budget exhaustion until the operator decides.

Campaign workers run in a child process; Observatory writes decisions into
``<run_dir>/control/budget_gate.json``. The mission loop polls that file.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

DecisionAction = Literal["grant_limited", "grant_unlimited", "abort"]

# Line D PREREG appendix A: billing outage is HOLD, never grant.
BILLING_MARKERS = (
    "HTTP 402",
    "http 402",
    "Error code: 402",
    "error code: 402",
    "Insufficient Balance",
    "insufficient balance",
    "insufficient_balance",
)

GATE_REL = Path("control") / "budget_gate.json"
METER_REL = Path("control") / "schedule_meter.json"

REASON_LABELS = {
    "tick_budget_exhausted": "scheduling fuel exhausted (internal engine steps)",
    "budget_exhausted": "coordination rounds exhausted",
    "wall_clock_exhausted": "wall-clock budget exhausted",
    "assessor_envelope_repair": "assessor seat returned a malformed envelope repeatedly",
    "human_gate": "the institution requires human confirmation",
    "worker_escalate": "a working seat asked for human help",
    "worker_retry_exhausted": "a working seat exhausted its self-repair attempts",
    "billing_402": "account out of credit (HTTP 402)",
    "token_fuse": "per-seed token fuse tripped",
    "wall_fuse": "per-seed wall-clock fuse tripped",
}

REASON_BLURBS = {
    "tick_budget_exhausted": (
        "Only reachable under the hard-gate mode (tests / SCITEAM_TICK_HARD_GATE). "
        "The default path tops scheduling steps up automatically; a run should "
        "never stop on ticks alone."
    ),
    "budget_exhausted": "Coordination-round cap reached. Paused — extend the budget or abort.",
    "wall_clock_exhausted": "Wall-clock cap reached. Paused — extend the budget or abort.",
    "assessor_envelope_repair": (
        "The assessor seat repeatedly failed to return valid round_assessment JSON. "
        "Extend the budget to let it retry, or abort and fix the skill/prompt first — "
        "this is an envelope failure, not a scientific result."
    ),
    "human_gate": "The paradigm declares that closing requires human confirmation.",
    "worker_escalate": (
        "The working seat attached its diagnosis and attempt log; supply constraints, "
        "resources, or a ruling."
    ),
    "worker_retry_exhausted": (
        "The working seat changed approach several times and still cannot satisfy the "
        "work-item contract; authorize continuation, add guidance, or abort."
    ),
    "billing_402": (
        "Diagnosis contains HTTP 402 / Insufficient Balance. This must HOLD; granting is "
        "forbidden. The campaign stays waiting_user until credit is restored, and this "
        "split is marked contaminated."
    ),
    "token_fuse": "This seed hit its token ceiling and was paused automatically.",
    "wall_fuse": "This seed hit its wall-clock ceiling and was paused automatically.",
}


def is_billing_failure(text: str) -> bool:
    """True when a worker diagnosis is an account-balance outage (HTTP 402)."""
    blob = text or ""
    return any(marker in blob for marker in BILLING_MARKERS)


def grant_forbidden(gate: dict[str, Any] | None) -> bool:
    """Grant is illegal on billing HOLD or seed-cost fuses."""
    if not gate:
        return False
    if gate.get("grant_forbidden") or gate.get("reason") in {
        "billing_402",
        "token_fuse",
        "wall_fuse",
    }:
        return True
    blob = f"{gate.get('reason', '')} {gate.get('detail', '')}"
    return is_billing_failure(blob)


def classify_hold_reason(*, reason: str, detail: str = "") -> str:
    """Normalize escalate/fuse reasons. Billing always wins over worker_escalate."""
    blob = f"{reason} {detail}"
    if is_billing_failure(blob) or reason == "billing_402":
        return "billing_402"
    if reason in {"token_fuse", "wall_fuse"}:
        return reason
    return reason


def check_seed_fuse(
    *,
    tokens: int,
    wall_seconds: float,
    token_cap: int,
    wall_cap_s: float,
) -> str | None:
    """Seed-level cost fuse. First match wins (token before wall)."""
    if int(tokens) > int(token_cap):
        return "token_fuse"
    if float(wall_seconds) > float(wall_cap_s):
        return "wall_fuse"
    return None


def gate_enabled() -> bool:
    """Default on; set ``SCITEAM_BUDGET_GATE=0`` to auto-fail (tests / batch)."""
    raw = (os.environ.get("SCITEAM_BUDGET_GATE") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def gate_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / GATE_REL


def read_gate(run_dir: Path | str) -> dict[str, Any] | None:
    path = gate_path(run_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def open_gate(
    run_dir: Path | str,
    *,
    mission_id: str,
    team_run_id: str,
    reason: str,
    rounds_used: int,
    current_max_iterations: int,
    detail: str = "",
    ticks_used: int | None = None,
    tick_budget: int | None = None,
) -> dict[str, Any]:
    """Write a pending gate request (idempotent while still pending)."""
    path = gate_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_gate(run_dir)
    if (
        existing
        and existing.get("status") == "pending"
        and existing.get("mission_id") == mission_id
        and existing.get("team_run_id") == team_run_id
    ):
        return existing
    hold_reason = classify_hold_reason(reason=reason, detail=detail)
    payload: dict[str, Any] = {
        "status": "pending",
        "mission_id": mission_id,
        "team_run_id": team_run_id,
        "reason": hold_reason,
        "rounds_used": int(rounds_used),
        "current_max_iterations": int(current_max_iterations),
        "detail": detail,
        "created_at": datetime.now(UTC).isoformat(),
        "decision": None,
        "hold": hold_reason in {"billing_402", "token_fuse", "wall_fuse"},
        "grant_forbidden": hold_reason in {"billing_402", "token_fuse", "wall_fuse"},
    }
    if ticks_used is not None:
        payload["ticks_used"] = int(ticks_used)
    if tick_budget is not None:
        payload["tick_budget"] = int(tick_budget)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def decide_gate(
    run_dir: Path | str,
    *,
    action: DecisionAction,
    extra_iterations: int = 0,
    operator_note: str = "",
) -> dict[str, Any]:
    """Record an operator decision on the pending gate."""
    if action not in {"grant_limited", "grant_unlimited", "abort"}:
        raise ValueError(f"invalid budget-gate action: {action}")
    if action == "grant_limited" and int(extra_iterations) < 1:
        raise ValueError("grant_limited requires extra_iterations >= 1")
    current = read_gate(run_dir)
    if current is None:
        raise FileNotFoundError("no budget gate pending for this run")
    if current.get("status") != "pending":
        raise RuntimeError(f"budget gate already decided: {current.get('status')}")
    if action in {"grant_limited", "grant_unlimited"} and grant_forbidden(current):
        raise RuntimeError(
            "HOLD: billing or seed-cost fuse — do not grant; mark split contaminated"
        )
    decision = {
        "action": action,
        "extra_iterations": int(extra_iterations) if action == "grant_limited" else 0,
        "operator_note": operator_note,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    current["status"] = "decided"
    current["decision"] = decision
    path = gate_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def clear_gate(run_dir: Path | str) -> None:
    path = gate_path(run_dir)
    if path.is_file():
        path.unlink()


def _parse_ticks_from_detail(detail: str) -> int | None:
    """Best-effort parse ``(... N ticks, ...)`` from older gate files."""
    import re

    m = re.search(r"\((\d+)\s+ticks", detail or "")
    return int(m.group(1)) if m else None


def pending_summary(run_dir: Path | str) -> dict[str, Any] | None:
    """UI-facing snapshot; only returns when status is pending."""
    data = read_gate(run_dir)
    if not data or data.get("status") != "pending":
        return None
    ticks_used = data.get("ticks_used")
    if ticks_used is None:
        ticks_used = _parse_ticks_from_detail(str(data.get("detail") or ""))
    reason = str(data.get("reason") or "")
    tick_budget = data.get("tick_budget") or ticks_used
    return {
        "pending": True,
        "mission_id": data.get("mission_id"),
        "team_run_id": data.get("team_run_id"),
        "reason": reason,
        "reason_label": REASON_LABELS.get(reason, reason or "budget exhausted"),
        "reason_blurb": REASON_BLURBS.get(
            reason, "The campaign is paused, awaiting human authorization."
        ),
        "rounds_used": data.get("rounds_used"),
        "current_max_iterations": data.get("current_max_iterations"),
        "ticks_used": ticks_used,
        "tick_budget": tick_budget,
        "tick_pct": (
            int(100 * int(ticks_used) / int(tick_budget))
            if ticks_used is not None and tick_budget
            else None
        ),
        "detail": data.get("detail") or "",
        "created_at": data.get("created_at"),
    }


def write_schedule_meter(
    run_dir: Path | str,
    *,
    mission_id: str,
    team_run_id: str,
    ticks_used: int,
    tick_budget: int,
    ticks_remaining: int,
    rounds_used: int,
    round_cap: int,
    n_agents: int = 0,
    state: str = "",
) -> None:
    """Live meter so Observatory can show schedule oil before the gate fires."""
    path = Path(run_dir) / METER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = max(1, int(tick_budget))
    used = max(0, int(ticks_used))
    payload = {
        "mission_id": mission_id,
        "team_run_id": team_run_id,
        "ticks_used": used,
        "tick_budget": budget,
        "ticks_remaining": max(0, int(ticks_remaining)),
        "tick_pct": min(100, int(100 * used / budget)),
        "rounds_used": int(rounds_used),
        "round_cap": int(round_cap),
        "n_agents": int(n_agents),
        "state": state,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": (
            "Ticks are internal engine steps, recorded for observability. They are "
            "topped up softly by default; a run stops on coordination rounds, "
            "wall clock, or contract outcome — not on ticks."
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_schedule_meter(run_dir: Path | str) -> dict[str, Any] | None:
    path = Path(run_dir) / METER_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
