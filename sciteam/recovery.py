"""Team/Mission cold-start recovery — 28-sciteam-productization-roadmap.md §7.2 (E7).

`sciteam.orchestrator.TeamOrchestrator` keeps `TeamRun`/`WorkBoard`/`DepGraph`
in process memory (`self._store`, `self._boards`, `self._deps`); a killed and
restarted process starts those empty. `sciteam.persist.snapshot_team_run`
already writes `team_run.json`/`board.json`/`deps.jsonl` after every tick —
this module is the missing other half: reading that snapshot back into a
fresh orchestrator so a cold start observes the same board/dep/run state a
live process would have, and never re-invokes the runner for a work item
that already reached a terminal (DONE/FAILED) state.

What is, and is not, recovered here:

* `TeamRun` (state, active_round, agents, full metadata) — from
  `team_run.json`.
* `WorkBoard` (every `WorkItem`, verbatim state) — from `board.json`.
* `DepGraph` (edges) — from `deps.jsonl`.
* Per-agent `task_memory_dir` — nothing to recover; it is already a plain
  directory on disk, read fresh on every access.
* Session log cursor — nothing to recover here either; `FileSessionStore`
  (docs/SESSION-KERNEL.md) is itself resumable by `session_id` and is
  reattached by the composition root, not by this module.
* Undelivered peer/supervisor messages (`TeamOrchestrator._inboxes`) —
  best-effort only. `run.metadata["inbox_log"]` retains the last 100 sent
  messages; on cold resume every logged message with a positive `ttl` is
  treated as still-pending for its target. A message already drained
  before the crash may be redelivered. This is deliberately tolerated:
  inbox messages are coordination hints, not `counts-as` institutional
  effects, so at-least-once delivery here does not violate the "no
  replaying an effective action" invariant that motivates this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sciteam.board import WorkBoard
from sciteam.depgraph import DepGraph
from sciteam.models import TeamRun


class RecoveryError(RuntimeError):
    """Raised when a cold-start snapshot is missing or unreadable.

    Never silently returns an empty run in its place — an operator asking
    to resume a specific `team_run_id` that cannot be found must see a
    clear failure, not a fresh, indistinguishable-from-real run.
    """


@dataclass(slots=True)
class RecoveredTeamRun:
    run: TeamRun
    board: WorkBoard
    deps: DepGraph
    inboxes: dict[str, list[dict]]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"could not read snapshot file: {path}") from exc


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Torn write at the very end of the file (crash mid-append) —
            # tolerate a truncated last line, same discipline as
            # `sciteam.session.FileSessionStore`'s torn-write handling.
            continue
    return rows


def _rebuild_inboxes(run: TeamRun) -> dict[str, list[dict]]:
    boxes: dict[str, list[dict]] = {}
    for message in run.metadata.get("inbox_log") or []:
        if not isinstance(message, dict):
            continue
        if int(message.get("ttl") or 0) <= 0:
            continue
        target = str(message.get("target_agent") or "")
        if not target:
            continue
        boxes.setdefault(target, []).append(dict(message))
    return boxes


def load_team_run_snapshot(work_root: Path | str, run_id: str) -> RecoveredTeamRun:
    """Reconstruct `(TeamRun, WorkBoard, DepGraph, inboxes)` for `run_id`
    from `<work_root>/<run_id>/{team_run.json,board.json,deps.jsonl}`.

    Raises `RecoveryError` if `team_run.json` is missing/unreadable — a
    caller resuming a specific run_id must not silently receive a run that
    never happened.
    """
    root = Path(work_root) / run_id
    team_run_path = root / "team_run.json"
    if not team_run_path.is_file():
        raise RecoveryError(f"no team_run.json snapshot for run_id={run_id!r} under {root}")

    run = TeamRun.from_dict(_read_json(team_run_path))

    board = WorkBoard()
    board_path = root / "board.json"
    if board_path.is_file():
        board.load_list(_read_json(board_path))

    deps = DepGraph()
    deps.load_list(_read_jsonl(root / "deps.jsonl"))

    return RecoveredTeamRun(run=run, board=board, deps=deps, inboxes=_rebuild_inboxes(run))


def has_snapshot(work_root: Path | str, run_id: str) -> bool:
    return (Path(work_root) / run_id / "team_run.json").is_file()
