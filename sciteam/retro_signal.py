"""Line F O_RETRO_INVOKE signal (ENGINE_ISA.md §2.11).

Minimal-risk implementation: no new tool, no new capability, no change to
`llm_worker.py`/`mission.py`/`wizard_tools.py`. Every mission worker seat
already has an always-on `fs.write` tool scoped to its own mission workspace
(`sciteam/worker_common.py` FS_ALWAYS_CAPABILITIES; `sciteam/fs_tools.py`).
This module only:

1. Defines the signal's filename + schema (schemas/retro_request.schema.json)
   so a seat that writes it is making a *structured*, schema-checkable
   request — not free-text narration a harness silently ignores (the same
   G2 discipline as every other exit contract in this repo).
2. Provides a pure scanner the campaign harness calls *after* a run
   finishes, to decide whether to invoke `harness/run_retro_mission.py`
   (unchanged) instead of that being an unconditional operator CLI choice.

Whether the request is ever honored is entirely up to the harness step that
calls `scan_for_retro_requests`/`should_invoke_retro` — this module grants
no authority, only visibility. The retro mission it may trigger still goes
through the full existing amendment pipeline (preflight -> shadow ->
promotion); this module sits strictly upstream of all of that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

SIGNAL_FILENAME = "retro_request.json"

LAB_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = LAB_ROOT / "schemas" / "retro_request.schema.json"


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def write_retro_request(
    mission_workspace: Path | str,
    *,
    requested_by: str,
    reason: str,
    requested_at: str,
    target_asset_hint: str | None = None,
) -> Path:
    """Reference writer (matches what a seat's `fs.write` call would produce).

    Used by tests and by any harness-side synthetic smoke check; the agent
    itself only ever calls the existing generic `fs.write` tool with this
    same filename and shape — this function is not itself a new tool.
    """
    payload: dict[str, Any] = {
        "requested_by": requested_by,
        "reason": reason,
        "requested_at": requested_at,
    }
    if target_asset_hint is not None:
        payload["target_asset_hint"] = target_asset_hint
    jsonschema.Draft202012Validator(_schema()).validate(payload)
    path = Path(mission_workspace) / SIGNAL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@dataclass(frozen=True)
class RetroSignal:
    mission_id: str
    path: str
    payload: dict[str, Any]


def scan_for_retro_requests(run_dir: Path | str) -> list[RetroSignal]:
    """All schema-valid retro_request.json files under `<run_dir>/missions/*/`.

    Fail-closed per signal: a malformed/off-schema file is silently skipped
    (not raised) — a corrupt or prompt-injected file must never crash the
    harness step that decides whether to run a retrospective; it just does
    not count as a request. Order is deterministic (sorted by mission_id).
    """
    missions_dir = Path(run_dir) / "missions"
    if not missions_dir.is_dir():
        return []
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema)
    signals: list[RetroSignal] = []
    for mission_dir in sorted(p for p in missions_dir.iterdir() if p.is_dir()):
        signal_path = mission_dir / SIGNAL_FILENAME
        if not signal_path.is_file():
            continue
        try:
            payload = json.loads(signal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or validator.is_valid(payload) is False:
            continue
        signals.append(
            RetroSignal(mission_id=mission_dir.name, path=str(signal_path), payload=payload)
        )
    return signals


def should_invoke_retro(run_dir: Path | str) -> bool:
    """True iff at least one schema-valid retro request exists in this run."""
    return bool(scan_for_retro_requests(run_dir))


def first_request_delay(run_dir: Path | str, mission_order: list[str]) -> int | None:
    """Index (0-based) into `mission_order` of the first mission carrying a
    valid signal — the "首提案延迟" metric (lineF/PREREG.md §2). None when no
    signal exists, or when a signal's mission_id is absent from
    `mission_order` (caller-supplied ordering, e.g. from summary.json's
    `missions` list; a mismatch is a caller bug, not silently coerced)."""
    signaled = {s.mission_id for s in scan_for_retro_requests(run_dir)}
    if not signaled:
        return None
    for idx, mission_id in enumerate(mission_order):
        if mission_id in signaled:
            return idx
    return None
