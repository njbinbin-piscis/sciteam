"""Unit tests for `sciteam/harness/profile_router.py` (Task P1-2: 5-Gate ProfileRouter)."""

from __future__ import annotations

from sciteam.harness.config import HarnessConfig
from sciteam.harness.profile_router import ProfileRouter
from sciteam.kernel.events import EventStore


def test_profile_router_five_gates_and_budget_tiers(tmp_path):
    router_med = ProfileRouter(budget_tier="medium")

    # Gate 1: compact
    d1 = router_med.select_profile("Check remote server status with no local oracle available.")
    assert d1.profile_name == "compact"
    assert d1.reasoning_effort == "low"
    assert d1.finalize_after_tools == 0

    # Gate 2: research
    d2 = router_med.select_profile(
        "Perform a multi-source literature review and provenance alignment across ChEMBL and PubMed."
    )
    assert d2.profile_name == "research"
    assert d2.reasoning_effort == "high"

    # Gate 3: deep (scientific invariant / molecular breeding / drug discovery / CAE)
    d3 = router_med.select_profile(
        "Design a molecular breeding crossing plan satisfying QTL linkage drag and inbreeding invariants."
    )
    assert d3.profile_name == "deep"
    assert d3.reasoning_effort == "high"
    assert d3.finalize_after_tools == 0

    # Gate 4: lean
    d4 = router_med.select_profile("Simple single file format conversion from JSON to CSV.")
    assert d4.profile_name == "lean"
    assert d4.reasoning_effort == "low"
    assert d4.finalize_after_tools == 0

    # Gate 5: standard
    d5 = router_med.select_profile(
        "Implement the multi-module data pipeline for customer billing reconciliation across services and databases."
    )
    assert d5.profile_name == "standard"
    assert d5.reasoning_effort == "medium"
    assert d5.finalize_after_tools == 0

    # Lite mode down-shifts bounded deep task to standard ; no profile denies tools
    router_lite = ProfileRouter(budget_tier="lite")
    d_lite = router_lite.select_profile("Optimize drug discovery ADMET & hERG invariant.")
    assert d_lite.profile_name == "standard"
    assert d_lite.finalize_after_tools == 0

    # Bind to HarnessConfig and emit `profile_selected` to EventStore
    store = EventStore(tmp_path / "events.db")
    try:
        base_cfg = HarnessConfig(name="sci-agent", model="scripted")
        bound_cfg, route_dec = router_med.bind_to_harness_config(
            base_cfg,
            "Solve FEM von Mises thermal-structural design invariant",
            store=store,
        )
        assert bound_cfg.reasoning_effort == "high"
        assert bound_cfg.runtime_policy is not None
        assert bound_cfg.runtime_policy.active_profile == "deep"
        events = store.read_global(types={"profile_selected"})
        assert len(events) == 1
        assert events[0].payload["profile_name"] == route_dec.profile_name
    finally:
        store.close()


def test_research_projects_use_the_research_profile():
    d = ProfileRouter(fixed_profile="research").select_profile("Simple single file conversion.")
    assert d.profile_name == "research" and d.finalize_after_tools == 0
