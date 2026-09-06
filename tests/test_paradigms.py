"""Paradigm template assembly: every shipped YAML loads, validates, and drives."""

from __future__ import annotations

import pytest
from sciteam import (
    CoordinationSpec,
    CreateSpec,
    TeamOrchestrator,
    TeamRunState,
    load_team_configs,
    resolve_start_params,
)

from tests.conftest import PARADIGMS_DIR, ArtifactWritingRuntime

EXPECTED_PARADIGMS = {
    "pipeline_chain",
    "brainstorm_swarm",
    "debate_elo",
    "build_fix_loop",
    "survey_gather",
    "adversarial_pair",
    "write_atelier",
    "co_scientist_tournament",
    # planning_council is exercised by live campaigns (seed_open_research /
    # seed_pharma_plan); listing it here closes the last gap so all 19
    # catalog templates have a deterministic drive-to-completion test.
    "planning_council",
}


def test_catalog_has_at_least_four_heterogeneous_paradigms():
    configs = {c.team_id: c for c in load_team_configs(PARADIGMS_DIR)}
    assert set(configs) >= EXPECTED_PARADIGMS
    assert len(configs) >= 4
    # Heterogeneity: catalog spans multiple topologies and scheduling modes
    specs = [CoordinationSpec.from_dict(c.coordination) for c in configs.values()]
    topologies = {s.topology for s in specs}
    modes = {s.scheduling.mode for s in specs}
    aggregations = {s.aggregation for s in specs}
    assert len(topologies) >= 3
    assert len(modes) >= 2
    assert len(aggregations) >= 3


def test_every_paradigm_coordination_validates():
    for config in load_team_configs(PARADIGMS_DIR):
        spec = CoordinationSpec.from_dict(config.coordination)
        assert spec.to_dict()  # normalizes without raising
        assert config.members, config.team_id


def test_replica_expansion():
    from sciteam import build_registry

    registry = build_registry(PARADIGMS_DIR)
    agents, _ = resolve_start_params("debate_elo", registry=registry)
    keys = [a["agent_key"] for a in agents]
    assert keys[:4] == ["debater_1", "debater_2", "debater_3", "judge"]
    assert "round_assessor" in keys


def test_co_scientist_tournament_six_institutional_posts():
    """Line-A: six tournament posts are paradigm seats, not engine phase enums."""
    from sciteam import build_registry

    registry = build_registry(PARADIGMS_DIR)
    agents, coordination = resolve_start_params(
        "co_scientist_tournament", registry=registry
    )
    keys = [a["agent_key"] for a in agents]
    assert keys[:6] == [
        "generation",
        "reflection",
        "ranking",
        "evolution",
        "proximity",
        "meta_review",
    ]
    assert "round_assessor" in keys
    assert coordination.get("topology") == "loop"
    assert coordination.get("aggregation") == "meta_analysis"
    cfg = registry.get("co_scientist_tournament")
    assert cfg is not None
    assert "meta_review" in cfg.artifact_emit_roles()
    assert "round_assessor" in cfg.assess_roles()
    assert any(
        m.agent_key == "meta_review" and m.emits_exit_artifact for m in cfg.members
    )


@pytest.mark.parametrize("paradigm_id", sorted(EXPECTED_PARADIGMS))
async def test_every_paradigm_drives_to_completion(paradigm_id, tmp_path):
    from sciteam import build_registry

    registry = build_registry(PARADIGMS_DIR)
    agents, coordination = resolve_start_params(paradigm_id, registry=registry)
    runtime = ArtifactWritingRuntime({})
    orch = TeamOrchestrator(runtime=runtime, work_root=tmp_path)
    run = orch.create_run(
        CreateSpec(
            team_profile_id=paradigm_id,
            goal=f"assembly smoke for {paradigm_id}",
            agents=agents,
            coordination=coordination,
        )
    )
    result = await orch.drive(run.id, max_ticks=32)
    assert result.state == TeamRunState.COMPLETED
    production = [
        a
        for a in agents
        if not (
            a.get("may_assess_round")
            or "may_assess_round" in (a.get("authority") or [])
        )
    ]
    # Assessor seats are not planned into production waves.
    assert len(runtime.calls) == len(production)
    assert "round_assessor" not in runtime.calls
