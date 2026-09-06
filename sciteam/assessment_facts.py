"""Build opaque assessment observation packs (no decisions)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sciteam.coordination import CoordinationSpec
from sciteam.models import TeamRun

_MISSING = object()


def _resolve_pointer(data: Any, pointer: str) -> Any:
    if not pointer or pointer == "/":
        return data
    node = data
    for part in pointer.strip("/").split("/"):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _draft_paths(mission_dir: Path, globs: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in globs:
        p = str(pattern or "").strip()
        if not p:
            continue
        for hit in sorted(mission_dir.glob(p)):
            if not hit.is_file():
                continue
            key = str(hit.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(str(hit))
    return found


def _pipeline_order_ok(run: TeamRun, pipeline: list[str], wave: int) -> bool:
    """True when done/failed completion order among pipeline seats is non-decreasing."""
    if not pipeline:
        return True
    index = {role: i for i, role in enumerate(pipeline)}
    finished: list[tuple[int, str]] = []
    for task in run.tasks:
        if int(task.round_index or 0) != wave:
            continue
        if task.agent_key not in index:
            continue
        if task.state.value not in {"done", "failed"}:
            continue
        finished.append((index[task.agent_key], task.agent_key))
    if len(finished) < 2:
        return True
    order = [i for i, _ in finished]
    return order == sorted(order)


def build_assessment_facts(run: TeamRun) -> dict[str, Any]:
    """Pure observation pack. Never includes a decision."""
    metadata = dict(run.metadata or {})
    coord = CoordinationSpec.from_run_metadata(metadata)
    pipeline = list(coord.work_graph.role_pipeline or ())
    wave = int(run.active_round or 0)
    current = [t for t in run.tasks if int(t.round_index or 0) == wave]
    wave_tasks = [
        {
            "agent_key": t.agent_key,
            "state": t.state.value,
            "result_len": len(t.result or ""),
            "error": t.error,
            "retry_count": int((t.metadata or {}).get("retry_count") or 0),
            "reason_code": str((t.metadata or {}).get("last_reason_code") or ""),
            "diagnosis_ref": str((t.metadata or {}).get("diagnosis_ref") or ""),
        }
        for t in current
    ]

    artifact_path = str(metadata.get("artifact_path") or "")
    art = Path(artifact_path) if artifact_path else None
    artifact_exists = bool(art and art.is_file())
    artifact_size = int(art.stat().st_size) if artifact_exists and art else 0
    artifact_sha = _file_sha256(art) if artifact_exists and art else None

    gate = metadata.get("artifact_gate")
    gate_observed: Any = None
    if artifact_exists and art and isinstance(gate, dict) and gate.get("pointer"):
        try:
            payload = json.loads(art.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = _MISSING
        if payload is not _MISSING:
            value = _resolve_pointer(payload, str(gate["pointer"]))
            gate_observed = None if value is _MISSING else value

    globs_raw = metadata.get("draft_globs")
    if isinstance(globs_raw, (list, tuple)) and globs_raw:
        globs = [str(x) for x in globs_raw]
    else:
        globs = ["*_draft*.json", "*_draft.json"]
    mission_dir = art.parent if art else Path(run.work_root)
    drafts = _draft_paths(mission_dir, globs) if mission_dir.is_dir() else []

    charter = coord.charter
    stall = metadata.get("stagnation_max_rounds")
    if stall is None:
        stall = getattr(charter, "stagnation_max_rounds", 0) or None
    if stall is not None:
        try:
            stall_n: int | None = int(stall)
        except (TypeError, ValueError):
            stall_n = None
    else:
        stall_n = None
    if stall_n is not None and stall_n <= 0:
        stall_n = None

    assess_roles = [str(x) for x in (metadata.get("assess_roles") or []) if str(x).strip()]
    if not assess_roles:
        for agent in run.agents:
            auth = agent.profile.get("authority") or []
            if agent.profile.get("may_assess_round") or (
                isinstance(auth, (list, tuple)) and "may_assess_round" in auth
            ):
                for token in (agent.profile.get("role"), agent.agent_key):
                    t = str(token or "").strip()
                    if t and t not in assess_roles:
                        assess_roles.append(t)

    emit_roles = [str(x) for x in (metadata.get("artifact_emit_roles") or []) if str(x)]

    charter_excerpt = {
        "standing_goal": charter.standing_goal,
        "stagnation_max_rounds": getattr(charter, "stagnation_max_rounds", 0) or None,
        "institutions": list(getattr(charter, "institutions", ()) or ()),
    }
    protocol_text = str(metadata.get("protocol_text") or charter.standing_goal or "")

    facts: dict[str, Any] = {
        "active_round": wave,
        "max_iterations": int(coord.stopping.max_iterations or 0),
        "wave_tasks": wave_tasks,
        "pipeline": pipeline,
        "pipeline_order_ok": _pipeline_order_ok(run, pipeline, wave),
        "artifact_path": artifact_path,
        "artifact_exists": artifact_exists,
        "artifact_sha256": artifact_sha,
        "artifact_size": artifact_size,
        "draft_paths_found": drafts,
        "artifact_emit_roles": emit_roles,
        "assess_roles": assess_roles,
        "artifact_gate": gate if isinstance(gate, dict) else None,
        "gate_observed_value": gate_observed,
        "protocol_text": protocol_text,
        "charter_excerpt": charter_excerpt,
        "charter_stagnation_max_rounds": stall_n,
        "next_round_hint_prev": str(metadata.get("next_round_hint") or ""),
    }
    if "tool_audit_summary" in metadata:
        facts["tool_audit_summary"] = metadata.get("tool_audit_summary")
    return facts
