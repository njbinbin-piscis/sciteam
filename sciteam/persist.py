"""Persist team-run / board / checkpoints for human inspection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sciteam.board import WorkBoard
from sciteam.depgraph import DepGraph
from sciteam.models import AgentSlot, TeamRun


def _clip(text: str, n: int = 4000) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 20] + "\n...[truncated]"


def snapshot_team_run(
    run: TeamRun,
    *,
    board: WorkBoard | None = None,
    deps: DepGraph | None = None,
) -> None:
    """Write a full JSON snapshot + append newly done tasks to rounds.jsonl."""
    root = Path(run.work_root)
    root.mkdir(parents=True, exist_ok=True)
    snap = {
        "id": run.id,
        "goal": run.goal,
        "team_profile_id": run.team_profile_id,
        "state": run.state.value,
        "active_round": run.active_round,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metadata": {
            k: v
            for k, v in (run.metadata or {}).items()
            if k
            in {
                "mission_id",
                "mission_kind",
                "exit_contract",
                "skills_allowlist",
                "artifact_path",
                "artifact_gate",
                "artifact_source",
                "artifact_emit_roles",
                "assess_roles",
                "coordination",
                "next_round_hint",
                "last_assessment_reason",
                "last_aggregation",
                "budget_exhausted",
                "budget_extensions",
                "gate_reason",
                "heartbeats_used",
                "worker_retry_limit",
                "worker_escalation",
                "human_gate_pending",
                "human_gate_cleared",
            }
        },
        "agents": [
            {
                "agent_key": a.agent_key,
                "state": a.state.value,
                "work_dir": a.work_dir,
                "profile": a.profile,
                "replica_of": a.replica_of,
                "active_work_item_id": a.active_work_item_id,
            }
            for a in run.agents
        ],
        "tasks": [
            {
                "task_id": t.task_id,
                "agent_key": t.agent_key,
                "round_index": t.round_index,
                "state": t.state.value,
                "result": _clip(t.result, 8000),
                "error": t.error,
                "handoff_to": t.handoff_to,
                "metadata": dict(t.metadata),
            }
            for t in run.tasks
        ],
    }
    (root / "team_run.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if board is not None:
        (root / "board.json").write_text(
            json.dumps(board.to_list(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if deps is not None:
        deps_path = root / "deps.jsonl"
        deps_path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in deps.to_list()),
            encoding="utf-8",
        )

    marker = root / ".persisted_task_ids"
    done_ids = set()
    if marker.is_file():
        done_ids = {
            line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    rounds_path = root / "rounds.jsonl"
    new_ids: list[str] = []
    with rounds_path.open("a", encoding="utf-8") as fh:
        for t in run.tasks:
            if t.task_id in done_ids:
                continue
            if t.state.value not in {"done", "failed"}:
                continue
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                        "team_run_id": run.id,
                        "mission_id": (run.metadata or {}).get("mission_id"),
                        "round_index": t.round_index,
                        "task_id": t.task_id,
                        "agent_key": t.agent_key,
                        "state": t.state.value,
                        "result": _clip(t.result, 12000),
                        "error": t.error,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            new_ids.append(t.task_id)
    if new_ids:
        with marker.open("a", encoding="utf-8") as fh:
            for tid in new_ids:
                fh.write(tid + "\n")


def checkpoint_work_item(
    run: TeamRun,
    work_item_id: str,
    *,
    agent: AgentSlot | None = None,
) -> str:
    """Write a checkpoint JSON; return relative ref path."""
    root = Path(run.work_root) / "checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{work_item_id}.json"
    payload = {
        "work_item_id": work_item_id,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_key": agent.agent_key if agent else None,
        "work_dir": agent.work_dir if agent else None,
        "open_files": [],
        "summary": f"checkpoint for {work_item_id}",
    }
    if agent is not None:
        wd = Path(agent.work_dir)
        if wd.is_dir():
            payload["open_files"] = sorted(p.name for p in wd.iterdir() if p.is_file())[:50]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path.relative_to(run.work_root))


def append_agent_log(mission_dir: Path | str, record: dict[str, Any]) -> None:
    path = Path(mission_dir)
    path.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record.setdefault("ts", datetime.now(UTC).isoformat(timespec="seconds"))
    with (path / "agent_log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_agent_result_file(work_dir: Path | str, *, round_index: int, content: str) -> Path:
    d = Path(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"round_{int(round_index):03d}_result.md"
    out.write_text(content or "", encoding="utf-8")
    return out


def inner_loop_path(work_dir: Path | str, work_item_id: str) -> Path:
    """Per-work-item agent-loop trace (LLM turns + tool calls)."""
    safe = Path(str(work_item_id)).name or "unknown"
    return Path(work_dir) / f"inner_loop_{safe}.jsonl"


def append_inner_loop_trace(
    work_dir: Path | str,
    *,
    work_item_id: str,
    steps: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
    mission_dir: Path | str | None = None,
) -> Path | None:
    """Persist one subagent's inner loop (observe/act tool cycle) for replay.

    Primary path: ``<agent_work_dir>/inner_loop_<work_item_id>.jsonl`` (overwrite
    for that work item — one claim execution). Also appends a session envelope to
    ``missions/<id>/tool_trace.jsonl`` when ``mission_dir`` is set (no longer
    clobbers other agents' traces).
    """
    if not work_item_id or not steps:
        return None
    d = Path(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = inner_loop_path(d, work_item_id)
    header = {
        "kind": "inner_loop_session",
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "work_item_id": work_item_id,
        "n_steps": len(steps),
        **(meta or {}),
    }
    lines = [json.dumps(header, ensure_ascii=False)]
    for i, step in enumerate(steps, start=1):
        row = dict(step)
        row.setdefault("step", i)
        row.setdefault("work_item_id", work_item_id)
        lines.append(json.dumps(row, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if mission_dir is not None:
        mdir = Path(mission_dir)
        mdir.mkdir(parents=True, exist_ok=True)
        with (mdir / "tool_trace.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            for step in steps:
                if str(step.get("kind") or "") != "tool_call":
                    continue
                fh.write(
                    json.dumps(
                        {
                            "tool": step.get("tool"),
                            "arguments": step.get("arguments"),
                            "result_preview": step.get("result_preview"),
                            "work_item_id": work_item_id,
                            "agent_key": (meta or {}).get("agent_key"),
                            "round_index": (meta or {}).get("round_index"),
                            "step": step.get("step"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    return path
