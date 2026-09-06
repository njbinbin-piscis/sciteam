#!/usr/bin/env python3
"""Live-LLM campaign entry — bring your own model, one shared engine.

    export SCITEAM_LLM_BASE_URL=https://api.your-provider.com/v1
    export SCITEAM_LLM_API_KEY=...
    export SCITEAM_LLM_MODEL=your-model-id
    python3 harness/run_campaign.py --profile open --run-name my_run \\
        --goal "..."

This is the minimal, generic path: it wires the same primitives every test
in this repo exercises against fakes — ``Campaign`` -> ``AdaptiveCampaignPlanner``
-> ``MissionRunner`` -> ``LlmWorkerRuntime`` (a ``RuntimePort`` implementation) —
to a real OpenAI-compatible endpoint via ``sciteam.llm.LlmClient``. There is
exactly one profile shipped (``open``: an arbitrary operator research goal,
no domain-locked frozen eval); add your own by registering another
``CampaignProfile`` in ``harness/profiles.py`` and a seed function shaped
like ``harness/adapters/seed_open_research.py``.

To bring your own agent backend instead of the shipped ``LlmWorkerRuntime``,
implement ``sciteam.runtime.RuntimePort`` and pass it in place of
``llm_runtime`` below — that seam is deliberately the only place this script
knows about "which model/backend answers for a seat".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))
if str(LAB_ROOT / "harness") not in sys.path:
    sys.path.insert(0, str(LAB_ROOT / "harness"))

from sciteam import (  # noqa: E402
    Campaign,
    CampaignBudget,
    CampaignKB,
    CampaignTrace,
    ContractValidator,
    LlmClient,
    LlmWorkerRuntime,
    MissionRunner,
    PromptAssets,
    TeamOrchestrator,
    build_registry,
    make_contract_coordinator,
)

from harness.adapters.plan_loader import (  # noqa: E402
    build_planner_from_plan,
    resolve_research_plan,
)
from harness.profiles import get_profile, list_profiles  # noqa: E402

PARADIGMS_DIR = LAB_ROOT / "assets" / "teams" / "paradigms"
SCHEMAS_DIR = LAB_ROOT / "schemas"
PROMPTS_DIR = LAB_ROOT / "assets" / "prompts"
SKILLS_DIR = LAB_ROOT / "assets" / "skills"


async def run_open(
    run_name: str,
    *,
    goal: str,
    max_missions: int,
    max_output_tokens: int,
    max_iterations: int,
    max_wall_clock_seconds: int,
) -> dict:
    if not goal.strip():
        raise ValueError("--goal is required (the research question)")

    run_dir = LAB_ROOT / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    from harness.adapters.seed_open_research import seed_open_research_plan

    plan = resolve_research_plan(
        run_dir,
        campaign_id=run_name,
        seed_fn=lambda campaign_id: seed_open_research_plan(campaign_id=campaign_id, goal=goal),
    )

    client = LlmClient()  # reads SCITEAM_LLM_BASE_URL / _API_KEY / _MODEL
    llm_runtime = LlmWorkerRuntime(
        client=client,
        assets=PromptAssets(prompts_dir=PROMPTS_DIR, skills_dir=SKILLS_DIR),
        schemas_dir=SCHEMAS_DIR,
        max_tokens=max_output_tokens,
    )

    orchestrator = TeamOrchestrator(
        runtime=llm_runtime,
        work_root=run_dir / "workspace",
        # A judgment-stopping mission's round assessor seat is itself an LLM
        # call, not a scripted stub, when running against a real endpoint —
        # see `sciteam.round_assessor.make_contract_coordinator`.
        coordinator=make_contract_coordinator(
            live=True, runtime=llm_runtime, assets_root=LAB_ROOT / "assets"
        ),
    )
    runner = MissionRunner(
        orchestrator=orchestrator,
        paradigms=build_registry(PARADIGMS_DIR),
        validator=ContractValidator(SCHEMAS_DIR),
        artifacts_root=run_dir / "missions",
        skills_dir=SKILLS_DIR,
        prompts_dir=PROMPTS_DIR,
    )

    kb = CampaignKB(run_dir / "kb.jsonl")
    trace = CampaignTrace(run_dir / "trace.jsonl")
    planner = build_planner_from_plan(plan)
    os.environ.setdefault("SCITEAM_MAX_ITERATIONS", str(int(max_iterations)))
    campaign = Campaign(
        campaign_id=run_name,
        goal=goal,
        planner=planner,
        runner=runner,
        kb=kb,
        trace=trace,
        budget=CampaignBudget(
            max_missions=max_missions,
            max_wall_clock_seconds=max_wall_clock_seconds,
        ),
        token_meter=lambda: client.usage.total_tokens,
    )
    result = await campaign.run()

    summary = {
        "campaign_id": run_name,
        "goal": goal,
        "model": client.config.model,
        "stop_reason": result.stop_reason,
        "missions": [o.to_dict() for o in result.outcomes],
        "paradigms_used": trace.paradigms_used(),
        "contract_closure": trace.contract_closure(),
        "usage": {
            "requests": client.usage.requests,
            "total_tokens": client.usage.total_tokens,
        },
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="open", help="adapter pack id")
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--goal", default="")
    parser.add_argument("--max-missions", type=int, default=16)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-iterations", type=int, default=48)
    parser.add_argument("--wall-clock-hours", type=float, default=8.0)
    args = parser.parse_args()

    if args.list_profiles:
        for p in list_profiles():
            print(f"{p['id']}: {p['label']}")
            print(f"  engine: {p['engine']}")
            print(f"  seed:   {p['seed']}")
            print(f"  note:   {p['notes']}")
        return

    profile = get_profile(args.profile)
    if profile.id != "open":
        raise SystemExit(
            f"profile {profile.id!r} is registered but has no execution path in "
            "this script yet — only 'open' is wired. Add one the same way "
            "run_open() is wired, or register a RuntimePort plugin."
        )
    run_name = args.run_name or f"camp_open_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    summary = asyncio.run(
        run_open(
            run_name,
            goal=args.goal,
            max_missions=args.max_missions,
            max_output_tokens=args.max_output_tokens,
            max_iterations=args.max_iterations,
            max_wall_clock_seconds=int(args.wall_clock_hours * 3600),
        )
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
