#!/usr/bin/env python3
"""Unified campaign entry — one engine, profile-selected adapters.

    python3 harness/run_campaign.py --list-profiles
    python3 harness/run_campaign.py --profile mini --run-name camp_mini_x
    python3 harness/run_campaign.py --profile pharma --data-dir ... --run-name camp_pharma_x

Profiles choose seed plan + eval adapter only. Orchestration is always:
Campaign → AdaptiveCampaignPlanner → MissionRunner → employment loop.

Legacy ``run_campaign_mini.py`` / ``run_campaign_pharma.py`` remain callable
composition roots while the merge completes; operators should prefer this entry.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))
if str(LAB_ROOT / "harness") not in sys.path:
    sys.path.insert(0, str(LAB_ROOT / "harness"))

from harness.profiles import get_profile, list_profiles  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="", help="adapter pack id")
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--goal", default="")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--max-missions", type=int, default=0)
    parser.add_argument("--max-output-tokens", type=int, default=0)
    parser.add_argument("--max-iterations", type=int, default=0)
    parser.add_argument("--wall-clock-hours", type=float, default=0.0)
    parser.add_argument("--arm", choices=("own", "pi", "dsh"), default="own")
    args = parser.parse_args()

    if args.list_profiles:
        for p in list_profiles():
            print(f"{p['id']}: {p['label']}")
            print(f"  engine: {p['engine']}")
            print(f"  seed:   {p['seed']}")
            print(f"  eval:   {p['eval_kind']}")
            print(f"  note:   {p['notes']}")
        return

    if not args.profile:
        parser.error("--profile is required (or pass --list-profiles)")

    profile = get_profile(args.profile)
    run_name = args.run_name or f"camp_{profile.id}_auto"
    goal = args.goal or profile.default_goal

    if profile.id == "open":
        from harness.run_campaign_open import run as run_open

        if not (args.goal or "").strip():
            parser.error("open profile requires an explicit --goal (the research question)")
        asyncio.run(
            run_open(
                run_name,
                goal=args.goal.strip(),
                max_missions=args.max_missions or 16,
                max_output_tokens=args.max_output_tokens or 8192,
                max_iterations=args.max_iterations or 48,
                max_wall_clock_seconds=int((args.wall_clock_hours or 8) * 3600),
            )
        )
        return

    if profile.id == "mini":
        from harness.run_campaign_mini import run as run_mini

        asyncio.run(
            run_mini(
                run_name,
                goal=goal,
                max_missions=args.max_missions or 8,
                max_output_tokens=args.max_output_tokens or 32768,
                max_iterations=args.max_iterations or 24,
                max_wall_clock_seconds=int((args.wall_clock_hours or 4) * 3600),
                arm=args.arm,
                condition="mini_kvcache_institutions",
            )
        )
        return

    if profile.id == "covering":
        from harness.adapters.seed_covering import seed_covering_plan
        from harness.run_campaign_mini import run as run_mini

        asyncio.run(
            run_mini(
                run_name,
                goal=goal,
                max_missions=args.max_missions or 8,
                max_output_tokens=args.max_output_tokens or 32768,
                max_iterations=args.max_iterations or 24,
                max_wall_clock_seconds=int((args.wall_clock_hours or 8) * 3600),
                seed_fn=seed_covering_plan,
                brief_title="Covering discovery",
                arm=args.arm,
                condition="covering",
            )
        )
        return

    if profile.id == "pharma":
        from harness.run_campaign_pharma import run as run_pharma

        raw = args.data_dir or "runs/sweep_al_v2/datasets/Lipophilicity_AstraZeneca"
        data_dir = Path(raw)
        if not data_dir.is_absolute():
            data_dir = (LAB_ROOT / data_dir).resolve()
        asyncio.run(
            run_pharma(
                run_name,
                data_dir=data_dir,
                goal=goal,
                max_missions=args.max_missions or 24,
                max_output_tokens=args.max_output_tokens or 16384,
                max_wall_clock_seconds=int((args.wall_clock_hours or 48) * 3600),
            )
        )
        return

    raise SystemExit(f"profile {profile.id} not wired")


if __name__ == "__main__":
    main()
