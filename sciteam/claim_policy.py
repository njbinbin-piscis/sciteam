"""Claim policies: scripted auto-claim and optional LLM decide."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sciteam.environment import EnvSnapshot


class ClaimActionKind(StrEnum):
    REST = "rest"
    CLAIM = "claim"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ClaimDecision:
    action: ClaimActionKind
    work_item_id: str | None = None
    reason: str = ""
    target: str | None = None
    question: str = ""
    evidence_refs: tuple[str, ...] = ()


class ClaimPolicy(Protocol):
    async def decide(self, snapshot: EnvSnapshot) -> ClaimDecision: ...


class ScriptedClaimPolicy:
    """Always claim the highest-priority matching ready item; else rest."""

    async def decide(self, snapshot: EnvSnapshot) -> ClaimDecision:
        if snapshot.self_state.value not in {"idle", "done"}:
            return ClaimDecision(ClaimActionKind.REST, reason="not idle")
        if not snapshot.ready:
            return ClaimDecision(ClaimActionKind.REST, reason="no ready work")
        item = snapshot.ready[0]
        return ClaimDecision(
            ClaimActionKind.CLAIM,
            work_item_id=item.id,
            reason="scripted auto-claim",
        )


class LlmClaimPolicy:
    """Ask LLM for a short JSON decision; fall back to scripted on failure."""

    def __init__(self, llm: Any, *, fallback: ClaimPolicy | None = None) -> None:
        self._llm = llm
        self._fallback = fallback or ScriptedClaimPolicy()

    async def decide(self, snapshot: EnvSnapshot) -> ClaimDecision:
        ready = [
            {"id": i.id, "priority": i.priority, "prompt": i.prompt[:200], "urgent": i.urgent}
            for i in snapshot.ready
        ]
        prompt = (
            "You are an employed agent deciding the next action.\n"
            f"Goal: {snapshot.goal}\n"
            f"Charter: {snapshot.charter}\n"
            f"Ready work: {json.dumps(ready)}\n"
            f"Inbox: {json.dumps(snapshot.inbox, ensure_ascii=False)[:4000]}\n"
            "Escalate only when self-repair cannot proceed; target may be an "
            "agent key, supervisor, peer, or human.\n"
            'Reply JSON only: {"action":"rest|claim|escalate",'
            '"work_item_id":null|"id","reason":"...",'
            '"target":null|"peer|supervisor|human|agent_key",'
            '"question":"","evidence_refs":[]}'
        )
        try:
            resp = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=256,
            )
            text = getattr(resp, "text", None) or getattr(resp, "content", None) or str(resp)
            data = json.loads(text[text.find("{") : text.rfind("}") + 1])
            action = ClaimActionKind(str(data.get("action") or "rest"))
            wid = data.get("work_item_id")
            if action == ClaimActionKind.CLAIM and not wid and snapshot.ready:
                wid = snapshot.ready[0].id
            return ClaimDecision(
                action=action,
                work_item_id=str(wid) if wid else None,
                reason=str(data.get("reason") or "llm"),
                target=(str(data["target"]) if data.get("target") else None),
                question=str(data.get("question") or ""),
                evidence_refs=tuple(str(x) for x in (data.get("evidence_refs") or []) if str(x)),
            )
        except Exception:  # noqa: BLE001
            return await self._fallback.decide(snapshot)
