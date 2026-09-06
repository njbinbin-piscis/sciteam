"""Coverage set (Table 4): external published systems as pure templates.

Each paradigm below expresses a published multi-agent system as a policy
template over the unchanged substrate. The invariant these tests protect is
SWARM-LAW-0: the paradigm loads, its CoordinationSpec validates, and it drives
to COMPLETED on the fake runtime without any engine branch keyed to the
pattern. If a new paradigm required engine code to reach completion, that would
falsify the pattern-completeness claim for these systems.
"""

from __future__ import annotations

import pytest
from sciteam import (
    CoordinationSpec,
    CreateSpec,
    TeamOrchestrator,
    TeamRunState,
    build_registry,
    resolve_start_params,
)

from tests.conftest import PARADIGMS_DIR, ArtifactWritingRuntime

# published system -> SciTeam coverage paradigm id
COVERAGE_PARADIGMS = {
    "co_scientist": "co_scientist_tournament",
    "robin": "robin_lab",
    "debate_symmetric": "debate",
    "metagpt_sop": "software_sop_chain",
    "handoff": "agent_handoff",
    "continuous": "standing_watch",
}


def test_coverage_catalog_present():
    registry = build_registry(PARADIGMS_DIR)
    for paradigm_id in COVERAGE_PARADIGMS.values():
        assert registry.get(paradigm_id) is not None, paradigm_id


def test_coverage_paradigms_validate_and_are_heterogeneous():
    registry = build_registry(PARADIGMS_DIR)
    specs = [
        CoordinationSpec.from_dict(registry.get(pid).coordination)
        for pid in COVERAGE_PARADIGMS.values()
    ]
    topologies = {s.topology for s in specs}
    aggregations = {s.aggregation for s in specs}
    # route (handoff), chain (sop), loop (continuous/co_scientist), parallel (debate/robin)
    assert len(topologies) >= 4, topologies
    # majority (debate), meta_analysis (robin/co_scientist), none (handoff/sop/continuous)
    assert len(aggregations) >= 2, aggregations
    # exactly one emit seat per paradigm
    for pid in COVERAGE_PARADIGMS.values():
        cfg = registry.get(pid)
        emit = [m for m in cfg.members if m.emits_exit_artifact]
        assert len(emit) == 1, (pid, [m.agent_key for m in emit])


def test_robin_declares_human_gate_via_escalate_primitive():
    """Robin's lab-in-the-loop gate is a policy flag realized by the substrate
    escalate primitive — not a new engine capability."""
    registry = build_registry(PARADIGMS_DIR)
    spec = CoordinationSpec.from_dict(registry.get("robin_lab").coordination)
    assert spec.human_gate is True
    assert spec.aggregation.value == "meta_analysis"
    assert spec.replicas.get("analyst", 0) >= 2


def test_handoff_is_route_topology_no_new_primitive():
    registry = build_registry(PARADIGMS_DIR)
    spec = CoordinationSpec.from_dict(registry.get("agent_handoff").coordination)
    assert spec.topology.value == "route"


def test_continuous_is_charter_policy():
    registry = build_registry(PARADIGMS_DIR)
    spec = CoordinationSpec.from_dict(registry.get("standing_watch").coordination)
    assert spec.charter.wake_on
    assert spec.charter.wake_interval_seconds > 0


@pytest.mark.parametrize("paradigm_id", sorted(set(COVERAGE_PARADIGMS.values())))
async def test_coverage_paradigm_drives_to_completion(paradigm_id, tmp_path):
    """SWARM-LAW-0: each coverage paradigm reaches COMPLETED on the unchanged
    engine using only its template declaration."""
    registry = build_registry(PARADIGMS_DIR)
    agents, coordination = resolve_start_params(paradigm_id, registry=registry)
    runtime = ArtifactWritingRuntime({})
    orch = TeamOrchestrator(runtime=runtime, work_root=tmp_path)
    run = orch.create_run(
        CreateSpec(
            team_profile_id=paradigm_id,
            goal=f"coverage smoke for {paradigm_id}",
            agents=agents,
            coordination=coordination,
        )
    )
    result = await orch.drive(run.id, max_ticks=48)
    if result.state == TeamRunState.WAITING_USER:
        # Routine lab-in-the-loop gate (Robin): a human intervention clears it,
        # then the run completes. The gate is a substrate feature (waiting_user
        # + human_gate_cleared), invoked by a policy flag — not engine surgery.
        gated = orch.require(run.id)
        assert gated.metadata.get("gate_reason") == "human_gate", gated.metadata
        gated.metadata["human_gate_cleared"] = True
        orch._store.save(gated)
        result = await orch.drive(run.id, max_ticks=48)
    assert result.state == TeamRunState.COMPLETED, (paradigm_id, result.state)
    production = [
        a
        for a in agents
        if not (a.get("may_assess_round") or "may_assess_round" in (a.get("authority") or []))
    ]
    assert len(runtime.calls) == len(production)
    assert "round_assessor" not in runtime.calls
