"""I_PROCESS_AUDIT veto -> mechanical I_DESIGN_FEEDBACK reopen (line A close-loop
gap #2, ENGINE_ISA.md §... previously marked both institutions ❌ "no wired
trigger anywhere in sciteam/" — see `sciteam/coordination.py`'s 2026-08-13
audit-finding comment).

Signal convention mirrors `sciteam/retro_signal.py::O_RETRO_INVOKE`: a seat
writes a schema-checked file via its existing always-on `fs.write` tool (no
new engine tool call surface); a harness-side scanner decides whether the
signal has any effect. The difference from a retro request is authority:
a retro request from anyone is worth looking at, but an audit veto only
mechanically reopens the target when the writer is a seat this institution's
own paradigm YAML has declared `may_veto_exit` for
(`sciteam.team_loader.TeamConfig.may_veto_roles`) — an unauthorized seat's
veto file is still schema-valid, still gets scanned, and is still silently
not honored. This is the "最小权限语义" requirement from the same close-loop
gap: an unauthorized seat's output does not gain institutional effect just
because it is well-formed.

The actual reopen is mechanical, not a judgment call: `VetoUpstreamReviser`
never asks an LLM whether to honor an authorized veto — it deterministically
turns it into a `REOPEN_UPSTREAM` action for
`sciteam.adaptive_planner.AdaptiveCampaignPlanner`, which already has a
fully-tested reopen/supersede primitive (`tests/test_reopen_upstream.py`)
that was, until this module, never wired to a live trigger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

SIGNAL_FILENAME = "audit_veto.json"

LAB_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = LAB_ROOT / "schemas" / "audit_veto.schema.json"


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def write_veto(
    mission_workspace: Path | str,
    *,
    target: str,
    reason_code: str,
    vetoed_by: str,
    vetoed_at: str,
    detail: str | None = None,
) -> Path:
    """Reference writer (matches what a seat's `fs.write` call would produce).

    Used by tests and harness-side synthetic smoke checks; the agent itself
    only ever calls the existing generic `fs.write` tool with this same
    filename and shape — this function is not itself a new tool.
    """
    payload: dict[str, Any] = {
        "target": target,
        "reason_code": reason_code,
        "vetoed_by": vetoed_by,
        "vetoed_at": vetoed_at,
    }
    if detail is not None:
        payload["detail"] = detail
    jsonschema.Draft202012Validator(_schema()).validate(payload)
    path = Path(mission_workspace) / SIGNAL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@dataclass(frozen=True)
class VetoSignal:
    mission_id: str
    path: str
    payload: dict[str, Any]

    @property
    def target(self) -> str:
        return str(self.payload["target"])

    @property
    def reason_code(self) -> str:
        return str(self.payload["reason_code"])

    @property
    def vetoed_by(self) -> str:
        return str(self.payload["vetoed_by"])


def scan_for_vetoes(run_dir: Path | str) -> list[VetoSignal]:
    """All schema-valid `audit_veto.json` files under `<run_dir>/missions/*/`.

    Fail-closed per signal (same discipline as `retro_signal.scan_for_retro_requests`):
    a malformed/off-schema file is silently skipped, never raised — a corrupt
    or prompt-injected file must never crash the harness step that decides
    whether to reopen anything; it just does not count as a veto. Order is
    deterministic (sorted by mission_id).
    """
    missions_dir = Path(run_dir) / "missions"
    if not missions_dir.is_dir():
        return []
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema)
    signals: list[VetoSignal] = []
    for mission_dir in sorted(p for p in missions_dir.iterdir() if p.is_dir()):
        signal_path = mission_dir / SIGNAL_FILENAME
        if not signal_path.is_file():
            continue
        try:
            payload = json.loads(signal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not validator.is_valid(payload):
            continue
        signals.append(
            VetoSignal(mission_id=mission_dir.name, path=str(signal_path), payload=payload)
        )
    return signals


class VetoUpstreamReviser:
    """PlanReviser: mechanically REOPEN_UPSTREAM for every fresh, authorized veto.

    Fits `sciteam.adaptive_planner.PlanReviser` (duck-typed `.revise(kb=,
    completed=, nodes=) -> list[dict]`); combine with other revisers via
    `sciteam.adaptive_planner.CompositeReviser`.

    Idempotent across repeated `revise()` calls (the planner calls this after
    every successful mission, not just once): each veto's `(mission_id,
    target)` pair is honored at most once, tracked in-memory. A veto from a
    seat outside `authorized_seats` is recorded as seen (so it is not
    re-evaluated every tick) but never turned into an action — the mechanical
    equivalent of "this submission carries no institutional authority".
    """

    def __init__(self, *, run_dir: Path | str, authorized_seats: frozenset[str]) -> None:
        self._run_dir = Path(run_dir)
        self._authorized_seats = frozenset(authorized_seats)
        self._seen: set[tuple[str, str]] = set()
        self.ignored_unauthorized: list[VetoSignal] = []

    def revise(
        self,
        *,
        kb: Any = None,
        completed: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        del kb, completed, nodes
        actions: list[dict[str, Any]] = []
        for signal in scan_for_vetoes(self._run_dir):
            key = (signal.mission_id, signal.target)
            if key in self._seen:
                continue
            self._seen.add(key)
            if signal.vetoed_by not in self._authorized_seats:
                self.ignored_unauthorized.append(signal)
                continue
            actions.append(
                {
                    "action": "REOPEN_UPSTREAM",
                    "target": signal.target,
                    "reason_code": signal.reason_code,
                    "requested_by": signal.vetoed_by,
                }
            )
        return actions
