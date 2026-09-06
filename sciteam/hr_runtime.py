"""LLM HR officer staffing sessions (observable, pack-backed).

G6: every mission roster is staffed by the ``hr_officer`` agent. The agent may
call HR tools (list templates, recruit/train instances). Sessions are written
under ``pack/hr_sessions/<mission_id>/`` so the observatory can show behaviour.

When no LLM client is available (dry-run / tests), a deterministic path still
writes a full session record marked ``mode=deterministic_fallback``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sciteam import asset_paths
from sciteam.experiment_pack import (
    PackError,
    append_hr_log,
    list_pack_agents,
    list_template_agents,
    materialize_pack,
    pack_root,
    read_pack_agent,
    recruit_agent,
    staff_roster_via_hr,
)
from sciteam.roster_policy import load_role_catalog, validate_roster


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


HR_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_role_templates",
            "description": "List system role templates available for recruitment.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pack_agents",
            "description": "List agent instances already in this experiment pack.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recruit_agent",
            "description": (
                "Recruit/train a pack agent instance from a template. "
                "Never use worker/stage_* names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_key": {"type": "string"},
                    "template_id": {"type": "string"},
                    "title": {"type": "string"},
                    "train_notes": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["agent_key"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_agent",
            "description": "Read a pack agent instance definition and bound skills.",
            "parameters": {
                "type": "object",
                "properties": {"agent_key": {"type": "string"}},
                "required": ["agent_key"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class StaffingResult:
    roster: list[dict[str, Any]]
    mode: str  # llm_hr | deterministic_fallback
    session_dir: str
    report_path: str
    rationale: str = ""
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roster": self.roster,
            "mode": self.mode,
            "session_dir": self.session_dir,
            "report_path": self.report_path,
            "rationale": self.rationale,
            "n_tool_calls": len(self.tool_trace),
            "n_transcript_turns": len(self.transcript),
        }


def sessions_dir(run_dir: Path | str) -> Path:
    return pack_root(run_dir) / "hr_sessions"


def list_hr_sessions(run_dir: Path | str) -> list[dict[str, Any]]:
    root = sessions_dir(run_dir)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        report = path / "staffing_report.json"
        rec: dict[str, Any] = {"session_id": path.name, "path": str(path)}
        if report.is_file():
            try:
                rec["report"] = json.loads(report.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rec["report"] = None
        transcript = path / "transcript.jsonl"
        rec["has_transcript"] = transcript.is_file()
        rec["has_tool_trace"] = (path / "tool_trace.jsonl").is_file()
        out.append(rec)
    return out


def read_hr_session(run_dir: Path | str, session_id: str) -> dict[str, Any]:
    path = sessions_dir(run_dir) / session_id
    if not path.is_dir():
        raise FileNotFoundError(session_id)
    report = {}
    report_path = path / "staffing_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    transcript: list[dict[str, Any]] = []
    tp = path / "transcript.jsonl"
    if tp.is_file():
        for line in tp.read_text(encoding="utf-8").splitlines():
            try:
                transcript.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    tools: list[dict[str, Any]] = []
    tt = path / "tool_trace.jsonl"
    if tt.is_file():
        for line in tt.read_text(encoding="utf-8").splitlines():
            try:
                tools.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    request = {}
    rq = path / "request.json"
    if rq.is_file():
        request = json.loads(rq.read_text(encoding="utf-8"))
    return {
        "session_id": session_id,
        "path": str(path),
        "request": request,
        "report": report,
        "transcript": transcript,
        "tool_trace": tools,
    }


def _run_hr_tool(run_dir: Path, name: str, args: dict[str, Any]) -> Any:
    if name == "list_role_templates":
        return list_template_agents()
    if name == "list_pack_agents":
        return list_pack_agents(run_dir)
    if name == "inspect_agent":
        key = str(args.get("agent_key") or "")
        return read_pack_agent(run_dir, key)
    if name == "recruit_agent":
        skills = args.get("skills")
        if skills is not None and not isinstance(skills, list):
            skills = None
        return recruit_agent(
            run_dir,
            agent_key=str(args.get("agent_key") or ""),
            template_id=str(args.get("template_id") or "") or None,
            title=str(args.get("title") or ""),
            train_notes=str(args.get("train_notes") or ""),
            skills=skills,
            overwrite=bool(args.get("overwrite")),
        )
    return {"error": f"unknown HR tool: {name}"}


def _seat_key(item: dict[str, Any]) -> str:
    return str(item.get("agent_key") or item.get("key") or "").strip()


def _deterministic_staff(
    run_dir: Path,
    seats: list[dict[str, Any]],
    *,
    mission_id: str,
    session_dir: Path,
    reason: str,
) -> StaffingResult:
    roster = staff_roster_via_hr(run_dir, seats, mission_id=mission_id)
    rationale = (
        f"{reason} Deterministic recruit_agent applied for missing seats; "
        "no LLM HR deliberation this session."
    )
    report = {
        "mission_id": mission_id,
        "mode": "deterministic_fallback",
        "at": _now(),
        "rationale": rationale,
        "roster": roster,
        "decisions": [
            {
                "agent_key": a["agent_key"],
                "template_id": a.get("template_id"),
                "action": "ensure_instance",
            }
            for a in roster
        ],
    }
    report_path = session_dir / "staffing_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (session_dir / "transcript.jsonl").write_text(
        json.dumps(
            {
                "role": "hr_officer",
                "mode": "deterministic_fallback",
                "content": rationale,
                "at": _now(),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    append_hr_log(
        run_dir,
        {
            "action": "staff_session",
            "mode": "deterministic_fallback",
            "mission_id": mission_id,
            "session_dir": str(session_dir),
            "agents": [a["agent_key"] for a in roster],
        },
    )
    return StaffingResult(
        roster=roster,
        mode="deterministic_fallback",
        session_dir=str(session_dir),
        report_path=str(report_path),
        rationale=rationale,
        transcript=[{"role": "hr_officer", "content": rationale}],
    )


async def run_hr_staffing_session(
    run_dir: Path | str,
    seats: list[dict[str, Any]],
    *,
    mission_id: str,
    mission_goal: str = "",
    client: Any | None = None,
    max_tool_rounds: int = 8,
) -> StaffingResult:
    """Run one observable HR staffing session for the given seats."""
    run_dir = Path(run_dir)
    if not pack_root(run_dir).is_dir():
        materialize_pack(run_dir)
    # Bootstrap HR officer instance (template copy) so the agent identity exists.
    if not (pack_root(run_dir) / "agents" / "hr_officer" / "AGENT.md").is_file():
        recruit_agent(run_dir, agent_key="hr_officer", template_id="hr_officer")

    session_id = f"{mission_id}_{datetime.now(UTC).strftime('%H%M%S')}"
    session_dir = sessions_dir(run_dir) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    request = {
        "mission_id": mission_id,
        "mission_goal": mission_goal,
        "seats_requested": seats,
        "at": _now(),
    }
    (session_dir / "request.json").write_text(
        json.dumps(request, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if client is None:
        return _deterministic_staff(
            run_dir,
            seats,
            mission_id=mission_id,
            session_dir=session_dir,
            reason="No LLM client bound for HR.",
        )

    catalog = load_role_catalog()
    skill_path = asset_paths.skills_dir() / "hr.recruit" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""
    agent_path = pack_root(run_dir) / "agents" / "hr_officer" / "AGENT.md"
    role_text = agent_path.read_text(encoding="utf-8") if agent_path.is_file() else "You are HR."
    if role_text.startswith("---"):
        parts = role_text.split("---", 2)
        if len(parts) >= 3:
            role_text = parts[2].lstrip("\n")

    system = (
        f"{role_text}\n\n"
        f"<skill id=\"hr.recruit\">\n{skill_text}\n</skill>\n\n"
        "You are staffing ONE mission. Use HR tools to inspect templates and "
        "recruit/train pack instances for every requested seat. "
        "Never use worker/stage_* names. When done, emit ONLY JSON:\n"
        '{"summary":"...","roster":[{"agent_key":"...","template_id":"...","train_notes":"..."}],'
        '"rationale":"..."}\n'
        "Every requested seat must appear in roster after you have called recruit_agent."
    )
    seat_lines = "\n".join(
        f"- { _seat_key(s) } (role={s.get('role') or _seat_key(s)}, name={s.get('name') or ''})"
        for s in seats
        if _seat_key(s)
    )
    user = (
        f"# Staffing request\nmission_id: {mission_id}\n"
        f"goal: {mission_goal}\n\n## Seats required\n{seat_lines}\n\n"
        f"Catalog size: {len(catalog)} templates.\n"
        "Call tools as needed, then finalize with the JSON envelope."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tool_trace: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = [
        {"role": "system", "content": system[:2000], "at": _now()},
        {"role": "user", "content": user, "at": _now()},
    ]

    final_text = ""
    try:
        for _ in range(max_tool_rounds + 1):
            resp = await client.acomplete(
                messages,
                temperature=0.3,
                max_tokens=4096,
                tools=HR_TOOL_DEFS,
                tool_choice="auto",
            )
            if resp.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": resp.content or "",
                        "tool_calls": resp.tool_calls,
                    }
                )
                transcript.append(
                    {
                        "role": "assistant",
                        "content": resp.content or "",
                        "tool_calls": resp.tool_calls,
                        "at": _now(),
                    }
                )
                for tc in resp.tool_calls:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        result = _run_hr_tool(run_dir, name, args)
                        ok = True
                        err = ""
                    except Exception as exc:  # noqa: BLE001
                        result = {"error": str(exc)}
                        ok = False
                        err = str(exc)
                    preview = json.dumps(result, ensure_ascii=False)[:500]
                    tool_trace.append(
                        {
                            "tool": name,
                            "arguments": args,
                            "ok": ok,
                            "error": err,
                            "result_preview": preview,
                            "at": _now(),
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(tc.get("id") or name),
                            "content": json.dumps(result, ensure_ascii=False)[:12000],
                        }
                    )
                    transcript.append(
                        {
                            "role": "tool",
                            "tool": name,
                            "arguments": args,
                            "result_preview": preview,
                            "at": _now(),
                        }
                    )
                continue
            final_text = resp.content or ""
            transcript.append({"role": "assistant", "content": final_text, "at": _now()})
            break
    except Exception as exc:  # noqa: BLE001
        return _deterministic_staff(
            run_dir,
            seats,
            mission_id=mission_id,
            session_dir=session_dir,
            reason=f"LLM HR session failed ({exc}).",
        )

    # Persist traces before interpreting envelope
    (session_dir / "tool_trace.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tool_trace) + "\n",
        encoding="utf-8",
    )
    (session_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in transcript) + "\n",
        encoding="utf-8",
    )

    # Ensure every seat exists even if the model forgot a recruit call.
    missing = []
    for seat in seats:
        key = _seat_key(seat)
        if not key:
            continue
        if not (pack_root(run_dir) / "agents" / key / "AGENT.md").is_file():
            missing.append(seat)
    for seat in missing:
        key = _seat_key(seat)
        role = str(seat.get("role") or key)
        recruit_agent(
            run_dir,
            agent_key=key,
            template_id=role,
            title=str(seat.get("name") or key),
            train_notes=f"Auto-completed by HR gate after LLM session (mission {mission_id}).",
        )
        tool_trace.append(
            {
                "tool": "recruit_agent",
                "arguments": {"agent_key": key, "template_id": role, "auto": True},
                "ok": True,
                "result_preview": "auto-complete missing seat",
                "at": _now(),
            }
        )

    roster: list[dict[str, Any]] = []
    for seat in seats:
        key = _seat_key(seat)
        if not key:
            continue
        detail = read_pack_agent(run_dir, key)
        meta = detail.get("meta") or {}
        roster.append(
            {
                "agent_key": key,
                "agent_id": key,
                "name": str(meta.get("title") or seat.get("name") or key),
                "role": str(seat.get("role") or key),
                "skills": list(meta.get("skills") or []),
                "staffed_by": "hr_officer",
                "template_id": meta.get("template_id") or key,
            }
        )
    violations = validate_roster(roster)
    if violations:
        raise PackError("; ".join(v.format() for v in violations))

    rationale = ""
    try:
        from sciteam.llm_worker import _extract_json

        env = _extract_json(final_text) or {}
        rationale = str(env.get("rationale") or env.get("summary") or "")
    except Exception:  # noqa: BLE001
        rationale = final_text[:1000]

    report = {
        "mission_id": mission_id,
        "mode": "llm_hr",
        "at": _now(),
        "rationale": rationale,
        "roster": roster,
        "auto_completed_seats": [_seat_key(s) for s in missing],
        "n_tool_calls": len(tool_trace),
        "tools_used": sorted({t["tool"] for t in tool_trace}),
    }
    report_path = session_dir / "staffing_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # refresh tool_trace file if auto-completed
    (session_dir / "tool_trace.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tool_trace) + "\n",
        encoding="utf-8",
    )
    append_hr_log(
        run_dir,
        {
            "action": "staff_session",
            "mode": "llm_hr",
            "mission_id": mission_id,
            "session_dir": str(session_dir),
            "agents": [a["agent_key"] for a in roster],
            "tools_used": report["tools_used"],
        },
    )
    return StaffingResult(
        roster=roster,
        mode="llm_hr",
        session_dir=str(session_dir),
        report_path=str(report_path),
        rationale=rationale,
        tool_trace=tool_trace,
        transcript=transcript,
    )


def hr_overview(run_dir: Path | str) -> dict[str, Any]:
    from sciteam.experiment_pack import pack_overview

    ov = pack_overview(run_dir)
    sessions = list_hr_sessions(run_dir)
    return {
        **ov,
        "sessions": sessions,
        "n_sessions": len(sessions),
        "latest_session": sessions[0] if sessions else None,
    }
