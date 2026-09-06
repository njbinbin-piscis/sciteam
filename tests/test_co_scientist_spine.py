"""Institutional spine for Line-A Co-Scientist anchor (template-only)."""

from __future__ import annotations

import json
from pathlib import Path

from sciteam.registry_catalog import (
    registered_exit_contract_ids,
    registered_paradigm_ids,
)

LAB = Path(__file__).resolve().parents[1]
TEMPLATES = LAB / "assets" / "templates"


def test_co_scientist_spine_binds_registered_tournament_paradigm():
    registered_paradigm_ids.cache_clear()
    registered_exit_contract_ids.cache_clear()
    plan = json.loads((TEMPLATES / "co_scientist_spine.plan.json").read_text(encoding="utf-8"))
    paradigms = registered_paradigm_ids()
    contracts = registered_exit_contract_ids()
    hyp = next(
        n.get("mission", n)
        for n in plan["missions"]
        if (n.get("mission") or n).get("id") == "m_hyp"
    )
    assert hyp["paradigm"] == "co_scientist_tournament"
    assert hyp["paradigm"] in paradigms
    assert hyp["exit_contract"] in contracts
    assert "m_hyp" in plan.get("locked_ids", [])


def test_co_scientist_meta_listed_for_wizard():
    meta = json.loads((TEMPLATES / "co_scientist_spine.meta.json").read_text(encoding="utf-8"))
    assert meta["id"] == "co_scientist_spine"
