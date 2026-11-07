"""Institution-Gated Business Write Tool Wrapper (`sciteam/harness/tools/gated.py`).

Bridges `side_effect="write"` business tools (`BusinessTool`) with `InstitutionGate` & registered
business validators (`RegisteredValidator`):
  1. Pre-execution deterministic validation: runs any `validators` attached to the write tool;
     if any validator fails, returns an immediate institutional denial (`decision="deny"`).
  2. Approval gate (`approver` role / human sign-off): if the tool or argument threshold requires
     human approval and has not yet been approved in `approval_ledger`, records a `pending_approval`
     entry and returns a structured pending notice without executing the side-effect.
  3. Once approved (or when no approval is required and all validators pass), executes the write
     tool and records the audit event in `approval_ledger`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.kernel.canonical import canonical_json, sha256_hex

BusinessTool = Any
RegisteredValidator = Any


@dataclass
class PendingWriteApproval:
    act_id: str
    tool_name: str
    arguments: dict[str, Any]
    approver: str
    rule_name: str
    status: str = "pending"  # "pending" | "approved" | "rejected"
    decided_by: str | None = None
    note: str = ""
    corrected_arguments: dict[str, Any] | None = None


class WriteApprovalLedger:
    """Tracks pending, approved, and rejected `side_effect="write"` tool invocations."""

    def __init__(self) -> None:
        self.items: dict[str, PendingWriteApproval] = {}

    def act_id_for(self, tool_name: str, arguments: dict[str, Any]) -> str:
        digest = sha256_hex(canonical_json({"tool": tool_name, "args": arguments}))[:12]
        return f"act_write_{tool_name}_{digest}"

    def request(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        approver: str,
        rule_name: str,
    ) -> PendingWriteApproval:
        aid = self.act_id_for(tool_name, arguments)
        if aid not in self.items:
            self.items[aid] = PendingWriteApproval(
                act_id=aid,
                tool_name=tool_name,
                arguments=dict(arguments),
                approver=approver,
                rule_name=rule_name,
            )
        return self.items[aid]

    def decide(
        self,
        act_id: str,
        *,
        decision: str,
        by: str,
        note: str = "",
        corrected_arguments: dict[str, Any] | None = None,
    ) -> PendingWriteApproval:
        if act_id not in self.items:
            raise KeyError(f"Unknown pending write act_id: {act_id}")
        item = self.items[act_id]
        if decision not in ("approve", "reject"):
            raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
        item.status = "approved" if decision == "approve" else "rejected"
        item.decided_by = by
        item.note = note
        if corrected_arguments is not None:
            item.corrected_arguments = dict(corrected_arguments)
        return item

    def pending_for(self, approver_role: str | None = None) -> list[PendingWriteApproval]:
        return [
            it
            for it in self.items.values()
            if it.status == "pending"
            and (approver_role is None or approver_role in it.approver)
        ]


class GatedBusinessTool:
    """Wraps a `BusinessTool` with deterministic validators and human-seat approval gating."""

    def __init__(
        self,
        inner: BusinessTool,
        *,
        validators: list[RegisteredValidator] | None = None,
        approval_predicate: Callable[[dict[str, Any]], bool] | None = None,
        approver: str = "role:owner",
        rule_name: str = "high_risk_write_gate",
        ledger: WriteApprovalLedger | None = None,
    ) -> None:
        self.inner = inner
        self.spec = inner.spec
        self.readonly = inner.readonly
        self.validators = list(validators or [])
        self.approval_predicate = approval_predicate
        self.approver = approver
        self.rule_name = rule_name
        self.ledger = ledger if ledger is not None else WriteApprovalLedger()

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 1. Run pre-execution deterministic validators
        for v in self.validators:
            ok, reason = await v.validate({"tool": self.inner.name, "arguments": args})
            if not ok:
                return ToolResult(
                    content=f"denied by institutional validator '{v.name}': {reason}",
                    is_error=True,
                    meta={
                        "decision": "deny",
                        "validator": v.name,
                        "reason": reason,
                    },
                )

        # 2. Check if human approval is required for this write invocation
        ledger: WriteApprovalLedger = ctx.extras.get("write_ledger") or self.ledger
        needs_approval = (
            not self.readonly
            and self.approval_predicate is not None
            and bool(self.approval_predicate(args))
        )
        if needs_approval:
            state = ledger.request(
                tool_name=self.inner.name,
                arguments=args,
                approver=self.approver,
                rule_name=self.rule_name,
            )
            if state.status == "pending":
                return ToolResult(
                    content=(
                        f"pending institutional approval (act_id={state.act_id}, "
                        f"rule={self.rule_name}, approver={self.approver}): waiting for human decision"
                    ),
                    is_error=False,
                    meta={
                        "decision": "pending",
                        "act_id": state.act_id,
                        "rule": self.rule_name,
                        "approver": self.approver,
                    },
                )
            if state.status == "rejected":
                return ToolResult(
                    content=(
                        f"denied by {state.decided_by} ({self.rule_name}): {state.note}"
                    ),
                    is_error=True,
                    meta={
                        "decision": "deny",
                        "act_id": state.act_id,
                        "rejected_by": state.decided_by,
                        "note": state.note,
                        "corrected_arguments": state.corrected_arguments,
                    },
                )
            # If approved and human supplied corrected arguments, use them
            if state.corrected_arguments is not None:
                args = state.corrected_arguments

        res = await self.inner.run(args, ctx)
        res.meta["decision"] = "allow"
        return res
