"""Evidence bundles and human-gated skill revisions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_EVIDENCE_NAMES = {
    "agent_log.jsonl",
    "tool_trace.jsonl",
    "rounds.jsonl",
    "team_run.json",
    "board.json",
}


class ProposalStatus(StrEnum):
    AWAITING_OPERATOR = "awaiting_operator"
    APPLIED = "applied"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence_bundle(run_dir: Path | str) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    events: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if (
            path.name not in _EVIDENCE_NAMES
            and not path.name.startswith("inner_loop_")
            and "kb" not in path.name.lower()
        ):
            continue
        relative = str(path.relative_to(root))
        files.append({"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size})
        if path.suffix == ".jsonl":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(
                    {
                        "ref": f"{relative}#L{index}",
                        "event": payload,
                    }
                )
    return {
        "run_dir": str(root),
        "created_at": datetime.now(UTC).isoformat(),
        "files": files,
        "events": events[-500:],
    }


def create_skill_proposal(
    *,
    run_dir: Path | str,
    skill_id: str,
    proposed_version: str,
    scope: str,
    rationale: str,
    replacement_content: str,
    evidence_refs: list[str],
) -> Path:
    """Write a proposal under the run; never mutate template assets."""
    root = Path(run_dir).resolve()
    bundle = build_evidence_bundle(root)
    available = {str(event["ref"]) for event in bundle["events"]}
    available.update(str(row["path"]) for row in bundle["files"])
    missing = [ref for ref in evidence_refs if ref not in available]
    if missing:
        raise ValueError(f"proposal cites missing evidence: {missing}")
    if not evidence_refs:
        raise ValueError("skill proposal requires at least one evidence reference")
    target = root / "skill_proposals"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_id = skill_id.replace("/", "_").replace("..", "_")
    path = target / f"{safe_id}_{stamp}.json"
    payload = {
        "status": ProposalStatus.AWAITING_OPERATOR.value,
        "skill_id": skill_id,
        "proposed_version": proposed_version,
        "scope": scope,
        "rationale": rationale,
        "evidence_refs": evidence_refs,
        "replacement_content": replacement_content,
        "evidence_bundle_sha256": hashlib.sha256(
            json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle_path = target / f"{safe_id}_{stamp}.evidence.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def approve_skill_proposal(
    proposal_path: Path | str,
    *,
    experiment_pack_dir: Path | str,
    operator: str,
) -> Path:
    """Apply an explicitly approved proposal to one experiment pack only."""
    path = Path(proposal_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        status = ProposalStatus(str(payload.get("status") or ""))
    except ValueError as exc:
        raise RuntimeError(f"unknown proposal status: {payload.get('status')}") from exc
    if status != ProposalStatus.AWAITING_OPERATOR:
        raise RuntimeError(f"proposal is not pending: {payload.get('status')}")
    if not operator.strip():
        raise ValueError("operator identity is required")
    skill_id = str(payload["skill_id"])
    content = str(payload.get("replacement_content") or "")
    if not content.strip():
        raise ValueError("approved proposal has no replacement content")
    target = Path(experiment_pack_dir) / "skills" / skill_id / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    payload["status"] = ProposalStatus.APPLIED.value
    payload["approved_by"] = operator
    payload["approved_at"] = datetime.now(UTC).isoformat()
    payload["applied_to"] = str(target)
    payload["applied_sha256"] = _sha256(target)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
