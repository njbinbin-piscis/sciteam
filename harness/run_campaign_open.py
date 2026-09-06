#!/usr/bin/env python3
"""Open research campaign — arbitrary operator goal, shared engine.

No domain-locked frozen eval. Judges start here to test the thesis that
harness + institutions can run multi-team science process on any standing goal.

    python3 harness/run_campaign.py --profile open --goal "..." --run-name camp_open_01

See JUDGE_PATH.md.
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

import importlib.util as _ilu  # noqa: E402

import canned_artifacts as canned  # noqa: E402
from dispatch import DispatchRuntime, harness_mission_ids  # noqa: E402
from fake_runtime import ArtifactWritingRuntime
from institutions import append_ledger, finalize_institutions  # noqa: E402
from sciteam import (  # noqa: E402
    Campaign,
    CampaignBudget,
    CampaignKB,
    CampaignTrace,
    ContractValidator,
    LlmClient,
    LlmWorkerRuntime,
    MissionOutcome,
    MissionRunner,
    MissionSpec,
    PlannerDecision,
    PromptAssets,
    TeamOrchestrator,
    build_registry,
    make_contract_coordinator,
)

from harness.adapters.plan_loader import (  # noqa: E402
    build_planner_from_plan,
    resolve_research_plan,
)
from harness.adapters.seed_open_research import seed_open_research_plan  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "check_no_phase_enum", LAB_ROOT / "scripts" / "check_no_phase_enum.py"
)
e0_guard = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(e0_guard)

sys.path.insert(0, str(LAB_ROOT / "scripts"))
from freeze_protocol import freeze as freeze_protocol  # noqa: E402

PARADIGMS = LAB_ROOT / "assets" / "teams" / "paradigms"
SCHEMAS = LAB_ROOT / "schemas"


class ContextInjectingPlanner:
    def __init__(
        self,
        inner,
        kb: CampaignKB,
        standing_goal: str,
        *,
        campaign_memory_path: Path | None = None,
    ) -> None:
        self._inner = inner
        self._kb = kb
        self._standing_goal = standing_goal
        self._artifacts: dict[str, dict] = {}
        self._campaign_memory_path = campaign_memory_path

    def observe(self, outcome: MissionOutcome) -> None:
        self._inner.observe(outcome)
        if outcome.contract_ok and outcome.artifact_path:
            path = Path(outcome.artifact_path)
            if path.is_file():
                base = outcome.mission_id.rsplit("_a", 1)[0]
                try:
                    self._artifacts[base] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return

    def hydrate_from_run_dir(self, run_dir: Path | str) -> int:
        n = 0
        hydrate = getattr(self._inner, "hydrate_from_run_dir", None)
        if callable(hydrate):
            n = int(hydrate(run_dir) or 0)
        # Reload prior artifacts so resumed missions still see upstream outputs.
        missions_root = Path(run_dir) / "missions"
        if missions_root.is_dir():
            for path in missions_root.glob("*/*.json"):
                base = path.parent.name
                if base in self._artifacts:
                    continue
                try:
                    self._artifacts[base] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
        return n

    def next_mission(self, *, last_outcome, kb, clock) -> PlannerDecision:
        decision = self._inner.next_mission(
            last_outcome=last_outcome, kb=kb, clock=clock
        )
        if decision.action != "run_mission" or decision.mission is None:
            return decision
        data = decision.mission.to_dict()
        inputs = dict(data.get("inputs") or {})
        inputs["prior_artifacts"] = self._artifacts
        inputs["standing_goal"] = self._standing_goal
        data["inputs"] = inputs
        metadata = dict(data.get("metadata") or {})
        metadata["kb_digest"] = self._kb.summary(max_per_kind=3)
        metadata["budget_snapshot"] = clock.snapshot()
        metadata["standing_goal"] = self._standing_goal
        if self._campaign_memory_path is not None:
            metadata["campaign_memory_path"] = str(self._campaign_memory_path)
        data["metadata"] = metadata
        return PlannerDecision(
            action=decision.action,
            mission=MissionSpec.from_dict(data),
            reason=decision.reason,
            reentry=decision.reentry,
            reentry_target=decision.reentry_target,
        )

    def plan_snapshot(self):
        return self._inner.plan_snapshot()


def _no_frozen_eval(problem_id, candidate, out, seed=None):
    raise RuntimeError(
        "open profile has no domain frozen eval; do not call eval.run_frozen. "
        f"problem_id={problem_id!r}"
    )


async def run(
    run_name: str,
    *,
    max_missions: int,
    max_output_tokens: int,
    goal: str | None = None,
    max_wall_clock_seconds: int = 0,
    max_iterations: int = 48,
) -> dict:
    os.environ.setdefault("SCITEAM_MAX_ITERATIONS", str(int(max_iterations)))
    os.environ.setdefault("SCITEAM_MAX_TOOL_ROUNDS", "500")

    run_dir = LAB_ROOT / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = run_name
    standing = (goal or "").strip()
    if not standing:
        raise ValueError("open profile requires a non-empty --goal (the research question)")

    manifest_before = run_dir / "engine_manifest_before.json"
    if not manifest_before.exists():
        e0_guard.write_manifest(manifest_before)
    e0_before_ok = e0_guard.check() == 0

    protocol_path = run_dir / "protocol.yaml"
    if not protocol_path.exists():
        freeze_protocol(campaign_id, run_dir)

    def _seed(*, campaign_id: str) -> dict:
        return seed_open_research_plan(campaign_id=campaign_id, goal=standing)

    plan = resolve_research_plan(run_dir, campaign_id=campaign_id, seed_fn=_seed)
    (run_dir / "operator_brief.md").write_text(
        f"# Open research\n\n## Goal\n\n{standing}\n",
        encoding="utf-8",
    )

    from sciteam.experiment_pack import materialize_pack

    if not (run_dir / "pack" / "manifest.json").is_file():
        materialize_pack(run_dir)

    client = LlmClient()
    llm_runtime = LlmWorkerRuntime(
        client=client,
        assets=PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
            pack_dir=(run_dir / "pack") if (run_dir / "pack").is_dir() else None,
        ),
        schemas_dir=SCHEMAS,
        max_tokens=max_output_tokens,
        eval_runner=_no_frozen_eval,
    )

    def campaign_outcome() -> str:
        # Open research has no frozen-eval-style objective pass/fail criterion,
        # so this profile's summary.json always reports "partial" rather than
        # positive/negative. (2026-08-13 audit finding: this used to be written
        # as two if-branches that both fell through to the same "partial"
        # return, which read as if the frame's requires_external_resources
        # field or m_write's presence were actually deciding the outcome —
        # they were not. Collapsed to a constant to stop that misreading;
        # summary.json["outcome"] value is unchanged.)
        return "partial"

    scripted = ArtifactWritingRuntime(
        {
            "m_protocol": canned.protocol_registration(campaign_id, protocol_path),
            # m_rules deliberately excluded (2026-08-14, audit A1 follow-up, same
            # fix as run_campaign_mini.py): the open-research seed plan already
            # declares m_rules as planning_council/rules.legislate with no
            # harness metadata ("Legislation runs on the LLM council, not the
            # scripted worker" — see seed_open_research.py comment right above
            # the mission dict), so this dict was the only thing overriding that
            # intent. Mini-profile live validation (runs/smoke_mrules_llm_20260814)
            # confirmed the mechanism produces schema-valid, contract-passing
            # campaign_rules.json with genuine red-team review; extending the
            # same fix here.
            "m_package": lambda meta: canned.package_manifest(
                campaign_id, run_dir, campaign_outcome()
            ),
        }
    )
    runtime = DispatchRuntime(
        llm_runtime,
        scripted,
        harness_ids=harness_mission_ids(plan) | frozenset(scripted.artifacts_by_mission),
    )
    orchestrator = TeamOrchestrator(
        runtime=runtime,
        work_root=run_dir / "workspace",
        coordinator=make_contract_coordinator(
            live=True,
            runtime=llm_runtime,
            assets_root=LAB_ROOT / "assets",
        ),
    )
    from sciteam.capabilities import RuntimeCapabilities

    runner = MissionRunner(
        orchestrator=orchestrator,
        paradigms=build_registry(PARADIGMS),
        validator=ContractValidator(SCHEMAS),
        artifacts_root=run_dir / "missions",
        skills_dir=LAB_ROOT / "assets" / "skills",
        capabilities=RuntimeCapabilities.lab(eval_runner=True),
        pack_dir=run_dir / "pack",
        hr_client=client,
        prompts_dir=LAB_ROOT / "assets" / "prompts",
    )
    kb = CampaignKB(run_dir / "kb.jsonl")
    trace = CampaignTrace(run_dir / "trace.jsonl")
    inner = build_planner_from_plan(plan)
    planner = ContextInjectingPlanner(
        inner,
        kb,
        standing_goal=standing,
        campaign_memory_path=run_dir / "campaign_memory.jsonl",
    )
    hydrated = planner.hydrate_from_run_dir(run_dir)
    if hydrated:
        trace.record(
            "planner_hydrated",
            campaign_id=campaign_id,
            resolved_missions=hydrated,
            note="resumed from on-disk mission artifacts after process restart",
        )

    campaign = Campaign(
        campaign_id=campaign_id,
        goal=standing,
        planner=planner,
        runner=runner,
        kb=kb,
        trace=trace,
        budget=CampaignBudget(
            max_missions=max_missions,
            max_wall_clock_seconds=max(0, int(max_wall_clock_seconds or 0)),
        ),
        token_meter=lambda: client.usage.total_tokens,
    )
    result = await campaign.run()

    exports = trace.export_summaries(run_dir / "exports")
    e0_after_ok = e0_guard.check() == 0
    diff_rc = e0_guard.diff_against(manifest_before)
    (run_dir / "engine_diff_stat.txt").write_text(
        (
            f"campaign: {campaign_id}\n"
            f"profile: open\n"
            f"E0 static scan before: {'PASS' if e0_before_ok else 'FAIL'}\n"
            f"E0 static scan after:  {'PASS' if e0_after_ok else 'FAIL'}\n"
            f"engine files changed during campaign: {'NO' if diff_rc == 0 else 'YES'}\n"
            f"planner: open research seed\n"
            f"llm_model: {client.config.model}\n"
        ),
        encoding="utf-8",
    )
    three_q = run_dir / "three_questions.md"
    if not three_q.exists():
        three_q.write_text(
            "# Three-questions (open research)\n\n"
            "| # | date | sub-pattern | pure template? | new schema? | engine change? |\n"
            "|---|------|-------------|----------------|-------------|----------------|\n"
            f"| 1 | {datetime.now(UTC).date().isoformat()} | open operator goal | yes | problem_frame/method_plan | **no** |\n",
            encoding="utf-8",
        )

    summary = {
        "condition": "open_research",
        "campaign_id": campaign_id,
        "model": client.config.model,
        "stop_reason": result.stop_reason,
        "outcome": campaign_outcome(),
        "missions": [o.to_dict() for o in result.outcomes],
        "metrics": {
            "E0_engine_fork": {
                "static_scan_pass": e0_before_ok and e0_after_ok,
                "engine_changed_during_campaign": diff_rc != 0,
            },
            "E1_paradigms_used": trace.paradigms_used(),
            "E3_contract_closure": trace.contract_closure(),
            "plan_snapshot": planner.plan_snapshot(),
        },
        "usage": {
            "requests": client.usage.requests,
            "total_tokens": client.usage.total_tokens,
        },
        "exports": {k: str(v) for k, v in exports.items()},
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    inst = finalize_institutions(run_dir, campaign_id=campaign_id)
    append_ledger(
        run_dir,
        {
            "event": "legislation_enacted",
            "campaign_id": campaign_id,
            "profile": "open",
            "institutions": inst,
        },
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--max-missions", type=int, default=16)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-iterations", type=int, default=48)
    parser.add_argument("--wall-clock-hours", type=float, default=8.0)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.run_name,
            goal=args.goal,
            max_missions=args.max_missions,
            max_output_tokens=args.max_output_tokens,
            max_iterations=args.max_iterations,
            max_wall_clock_seconds=int(args.wall_clock_hours * 3600),
        )
    )


if __name__ == "__main__":
    main()
