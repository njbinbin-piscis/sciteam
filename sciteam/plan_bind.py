"""Bind operator intent onto a registered ResearchPlan template spine.

Institutional (Line A): coordination structure (paradigm / exit_contract / skills /
deps) comes from shipped templates under assets/templates — never from an
engine-side synonym map of research-stage vocabulary. The operator (or wizard)
supplies the scientific goal; the template supplies the runnable institution.
"""

from __future__ import annotations

import json
from typing import Any

from sciteam.registry_catalog import (
    registered_exit_contract_ids,
    registered_paradigm_ids,
    registry_notes_for_plan,
)


def plan_uses_registered_institutions(plan: dict[str, Any] | None) -> bool:
    """True iff every mission paradigm + exit_contract is in the live registries."""
    return not registry_notes_for_plan(plan)


def bind_plan_to_template_spine(
    *,
    template_plan: dict[str, Any],
    goal: str,
    operator_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a runnable plan: spine structure + operator goal overlay.

    - Always take paradigm / exit_contract / skills_allowlist / deps / on_fail from
      the template spine (the institution).
    - Inject ``goal`` into ``{{OPERATOR_GOAL}}`` placeholders and ``operator_goal``.
    - If ``operator_plan`` has same mission ids, overlay longer goal prose only.
    - Copy budget / problem_choice notes from operator when present.
    """
    spine = json.loads(json.dumps(template_plan))
    g = (goal or "").strip()
    op = operator_plan if isinstance(operator_plan, dict) else {}

    op_goals: dict[str, str] = {}
    for node in op.get("missions") or []:
        if not isinstance(node, dict):
            continue
        mission = node.get("mission") if isinstance(node.get("mission"), dict) else node
        if not isinstance(mission, dict):
            continue
        mid = str(mission.get("id") or "").strip()
        gt = str(mission.get("goal") or "").strip()
        if mid and gt:
            op_goals[mid] = gt

    for node in spine.get("missions") or []:
        if not isinstance(node, dict):
            continue
        mission = node.get("mission")
        if not isinstance(mission, dict):
            continue
        mid = str(mission.get("id") or "").strip()
        goal_text = str(mission.get("goal") or "")
        if g and "{{OPERATOR_GOAL}}" in goal_text:
            mission["goal"] = goal_text.replace("{{OPERATOR_GOAL}}", g)
        elif mid in op_goals and len(op_goals[mid]) > len(goal_text):
            # Overlay operator prose onto the institutional slot — keep spine fields.
            mission["goal"] = op_goals[mid][:4000]
        elif g and "{{OPERATOR_GOAL}}" not in goal_text:
            mission["goal"] = f"{goal_text}\n\n## Operator research goal\n{g}"[:4000]

    if g:
        spine["operator_goal"] = g
        pc = spine.get("problem_choice")
        if isinstance(pc, dict):
            rationale = str(pc.get("rationale") or "").strip()
            addon = f"Operator goal:\n{g}"
            if addon not in rationale:
                pc["rationale"] = (rationale + "\n" + addon).strip()

    op_budget = op.get("budget")
    if isinstance(op_budget, dict) and op_budget:
        budget = dict(spine.get("budget") or {})
        budget.update({k: v for k, v in op_budget.items() if v is not None})
        spine["budget"] = budget

    return spine


def ensure_runnable_plan(
    plan: dict[str, Any] | None,
    *,
    goal: str,
    template_plan: dict[str, Any],
) -> dict[str, Any]:
    """If plan already uses registered ids, keep it (after goal inject on spine slots).

    Otherwise rebase onto ``template_plan`` — the institutional remedy when a
    wizard invents research-stage vocabulary instead of template ids.
    """
    paradigms = registered_paradigm_ids()
    contracts = registered_exit_contract_ids()
    if isinstance(plan, dict) and plan_uses_registered_institutions(plan):
        # Still inject goal into any remaining placeholders without changing ids.
        out = json.loads(json.dumps(plan))
        g = (goal or "").strip()
        if g:
            out["operator_goal"] = g
            for node in out.get("missions") or []:
                mission = (node or {}).get("mission") if isinstance(node, dict) else None
                if not isinstance(mission, dict):
                    continue
                gt = str(mission.get("goal") or "")
                if "{{OPERATOR_GOAL}}" in gt:
                    mission["goal"] = gt.replace("{{OPERATOR_GOAL}}", g)
            # Defend against empty registries in broken checkouts
            _ = paradigms, contracts
        return out
    return bind_plan_to_template_spine(
        template_plan=template_plan, goal=goal, operator_plan=plan
    )
