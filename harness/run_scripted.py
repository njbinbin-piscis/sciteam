"""Scripted team-run entry point for SciTeam experiments."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from sciteam import (  # noqa: E402
    AgentRunResult,
    CreateSpec,
    Run,
    RunSpec,
    RunState,
    TeamOrchestrator,
    build_registry,
    resolve_start_params,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_subagent(
        self,
        *,
        agent_id: str,
        task: str,
        work_dir: str,
        parent_run_id: str | None = None,
        depth: int = 0,
        context: dict | None = None,
    ):
        del work_dir, depth, task
        key = str((context or {}).get("team_agent_key") or agent_id)
        self.calls.append(key)
        run = Run(
            run_id=f"worker_{key}_{len(self.calls)}",
            spec=RunSpec(
                kind="team_worker",
                input=key,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
            ),
            state=RunState.SUCCEEDED,
            output=f"ok:{key}",
        )
        return AgentRunResult(run=run, output=run.output)


async def _run(team_id: str, goal: str, run_name: str) -> dict:
    assets = LAB_ROOT / "assets" / "teams" / "paradigms"
    runs = LAB_ROOT / "runs" / run_name
    work = runs / "workspace"
    runs.mkdir(parents=True, exist_ok=True)

    registry = build_registry(assets)
    # Fall back to parent teams dir for legacy smoke profiles.
    try:
        agents, coordination = resolve_start_params(team_id, registry=registry)
    except KeyError:
        registry = build_registry(LAB_ROOT / "assets" / "teams")
        agents, coordination = resolve_start_params(team_id, registry=registry)
    runtime = FakeRuntime()
    orch = TeamOrchestrator(runtime=runtime, work_root=work)
    created = orch.create_run(
        CreateSpec(
            team_profile_id=team_id,
            goal=goal,
            agents=agents,
            coordination=coordination,
        )
    )
    result = await orch.drive(created.id, max_ticks=16)
    final = orch.require(created.id)
    summary = {
        "team_id": team_id,
        "goal": goal,
        "team_run_id": created.id,
        "state": final.state.value,
        "dispatched": result.dispatched,
        "worker_calls": list(runtime.calls),
        "active_round": final.active_round,
        "out_dir": str(runs),
    }
    (runs / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="SciTeam scripted experiment runner")
    parser.add_argument("--team", default="algo_scientist_smoke")
    parser.add_argument("--goal", default="smoke: algorithm scientist pipeline")
    parser.add_argument("--run-name", default=None)
    args = parser.parse_args()
    run_name = args.run_name or datetime.now(UTC).strftime("run_%Y%m%dT%H%M%SZ")
    summary = asyncio.run(_run(args.team, args.goal, run_name))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
