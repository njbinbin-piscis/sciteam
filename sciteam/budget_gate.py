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
    "tick_budget_exhausted": "调度油箱耗尽（内部步数）",
    "budget_exhausted": "协调轮次用尽",
    "wall_clock_exhausted": "墙钟时间用尽",
    "assessor_envelope_repair": "裁决席信封多次不合格",
    "human_gate": "制度要求人工确认",
    "worker_escalate": "工作席请求人工协助",
    "worker_retry_exhausted": "工作席自修次数耗尽",
    "billing_402": "账户欠费（HTTP 402）",
    "token_fuse": "本 seed token 熔断",
    "wall_fuse": "本 seed 墙钟熔断",
}

REASON_BLURBS = {
    "tick_budget_exhausted": (
        "仅在硬门控模式（测试 / SCITEAM_TICK_HARD_GATE）下出现。"
        "默认生产路径会自动续杯调度步数，不应因 tick 停科研实验。"
    ),
    "budget_exhausted": "协调轮次已达上限，任务暂停，等你追加或中止。",
    "wall_clock_exhausted": "墙钟时间已达上限，任务暂停，等你追加或中止。",
    "assessor_envelope_repair": (
        "裁决席连续未能返回合法 round_assessment JSON。"
        "可追加轮次让席位重试，或中止以便改技能/提示词后再跑——这不是科学结论停机。"
    ),
    "human_gate": "范式声明需要人工确认后才能结项。",
    "worker_escalate": "工作席已附诊断与尝试记录，请补充约束、资源或裁决。",
    "worker_retry_exhausted": (
        "工作席已多次改变方法但仍不能满足工单契约；可授权继续、补充指导或中止。"
    ),
    "billing_402": (
        "诊断含 HTTP 402 / Insufficient Balance。必须 HOLD，禁止 grant。"
        "余额恢复前战役保持 waiting_user。该 split 标记 contaminated。"
    ),
    "token_fuse": "本 seed 的 token 上限已触顶，自动暂停，不继续烧。",
    "wall_fuse": "本 seed 的墙钟上限已触顶，自动暂停。",
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
        "reason_label": REASON_LABELS.get(reason, reason or "预算耗尽"),
        "reason_blurb": REASON_BLURBS.get(reason, "战役已暂停，等待人类授权。"),
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
            "调度油箱（tick）= 引擎内部步数（观测用）。"
            "默认软续杯，不因耗尽停实验；科学停机看协调轮/墙钟/契约。"
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
