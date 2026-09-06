"""Turn a validated ResearchPlan artifact into an AdaptiveCampaignPlanner.

This is the bridge that makes the *research plan itself* a policy: a Planning
Council mission emits a `research_plan.json`, and this loader hands it to the
generic DAG planner. No engine change is needed to run a different plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sciteam.adaptive_planner import AdaptiveCampaignPlanner, PlanReviser
from sciteam.plan_bind import ensure_runnable_plan, plan_uses_registered_institutions


def build_planner_from_plan(
    plan: dict[str, Any],
    *,
    reviser: PlanReviser | None = None,
    finalizer: dict[str, Any] | None = None,
) -> AdaptiveCampaignPlanner:
    budget = plan.get("budget") or {}
    return AdaptiveCampaignPlanner(
        {"missions": plan["missions"]},
        reviser=reviser,
        max_revisions=int(budget.get("max_revisions") or 0),
        locked_ids=tuple(plan.get("locked_ids") or ()),
        finalizer=finalizer,
    )


def load_plan_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve_research_plan(
    run_dir: Path | str,
    *,
    campaign_id: str,
    seed_fn,
) -> dict[str, Any]:
    """Prefer an operator-supplied research_plan.json; else call seed_fn and write it.

    If the operator plan uses ids outside the live paradigm/contract registries,
    rebase onto the seed/template spine (institutional bind — no synonym tables).
    """
    run_dir = Path(run_dir)
    path = run_dir / "research_plan.json"
    seed = seed_fn(campaign_id=campaign_id)
    goal = ""
    brief = run_dir / "operator_brief.json"
    if brief.is_file():
        try:
            goal = str(json.loads(brief.read_text(encoding="utf-8")).get("goal") or "")
        except (OSError, json.JSONDecodeError, TypeError):
            goal = ""

    if path.is_file():
        plan = load_plan_file(path)
        missions = plan.get("missions")
        if isinstance(missions, list) and missions:
            if not plan_uses_registered_institutions(plan):
                plan = ensure_runnable_plan(
                    plan,
                    goal=goal or str(plan.get("operator_goal") or ""),
                    template_plan=seed,
                )
            path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return plan

    plan = seed
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return plan
