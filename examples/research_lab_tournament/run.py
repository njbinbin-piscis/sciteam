"""Autonomous Research Lab Hypothesis Tournament — Reference Institution Deployment.

Demonstrates how Institution Engineering governs scientific discovery workflows:
  1. Generation proposes scientific hypotheses.
  2. Reflection Critic scrutinizes falsifiability and detects non-falsifiable claims.
  3. Reflection issues an authorized VETO against flawed hypotheses.
  4. Generation re-proposes a falsifiable, bounded hypothesis revision.
  5. Blind Ranking Judge evaluates and scores candidates.
  6. Proximity Auditor verifies novelty against prior art literature.
  7. Consensus Meta-Reviewer synthesizes the certified research direction.
  8. All steps recorded in a tamper-evident EventLog and exported to HTML.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sciteam.event_log import LEDGER_FILENAME, EventLog, verify_chain
from sciteam.ledger_view import export_html_view
from sciteam.team_loader import load_team_config


def run_research_tournament(output_dir: Path | str) -> dict[str, Any]:
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = Path(__file__).parent / "institution.yaml"
    team_cfg = load_team_config(manifest_path)

    ledger_path = out_dir / LEDGER_FILENAME
    ledger = EventLog(ledger_path, run_id=f"research_lab_{out_dir.name}")

    # 1. Run start and rule version
    ledger.append(
        "run_started",
        payload={
            "domain": "chemical_biology",
            "topic": "antiviral_rna_mechanisms",
            "team": team_cfg.team_id,
        },
    )
    ledger.set_rule_version("rule@research_lab_v1.0")

    # 2. Generation seat proposes hypothesis candidates H1 and H2
    ledger.append(
        "artifact_accepted",
        effect="accepted",
        seat="generation",
        authority="may_propose_hypothesis",
        payload={
            "hypothesis_id": "H1",
            "title": "G-quadruplex RNA stabilization selectively halts viral translation",
            "falsifiable_criterion": "IC50 shift > 5x in G4 mutant assay",
        },
    )
    ledger.append(
        "artifact_accepted",
        effect="accepted",
        seat="generation",
        authority="may_propose_hypothesis",
        payload={
            "hypothesis_id": "H2",
            "title": "Unbounded metabolic bypass fully rescues cell viability without cost",
            "falsifiable_criterion": "general improvement",  # Flaw: vague, unfalsifiable
        },
    )

    # 3. Reflection Critic identifies unfalsifiable assertion and issues authorized veto
    ledger.append(
        "veto_issued",
        effect="accepted",
        seat="reflection",
        authority="may_veto",
        payload={
            "subject": "H2",
            "reason": "epistemic_unfalsifiable_claim",
            "detail": "H2 claims unbounded benefit violating thermodynamic conservation limits",
        },
    )

    # 4. Institutional planner reopens H2 for bounded scientific revision
    ledger.append(
        "reopened",
        effect="accepted",
        seat="planner",
        authority="may_revise_plan",
        payload={"subject": "H2", "reason": "authorized epistemic veto upstream"},
    )

    # 5. Generation seat submits repaired hypothesis H2_revised
    ledger.append(
        "artifact_accepted",
        effect="accepted",
        seat="generation",
        authority="may_propose_hypothesis",
        payload={
            "hypothesis_id": "H2_revised",
            "title": (
                "Bounded glycolysis modulation shifts lactate/pyruvate ratio > 2.0 under hypoxia"
            ),
            "falsifiable_criterion": "Lactate flux assay p < 0.01",
        },
    )

    # 6. Reflection Critic passes both candidates
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="reflection",
        authority="may_critique",
        payload={"subject": "H1", "falsifiability_score": 0.92, "passed": True},
    )
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="reflection",
        authority="may_critique",
        payload={"subject": "H2_revised", "falsifiability_score": 0.88, "passed": True},
    )

    # 7. Blind Tournament Judge conducts pairwise ranking
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="ranking",
        authority="may_rank_candidates",
        payload={
            "tournament_winner": "H1",
            "elo_scores": {"H1": 1540, "H2_revised": 1460},
            "passed": True,
        },
    )

    # 8. Literature Proximity Auditor certifies novelty (>0.30 distance from PubMed)
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="proximity",
        authority="may_audit_prior_art",
        payload={
            "subject": "H1",
            "nearest_prior_art_cosine_distance": 0.42,
            "novelty_certified": True,
        },
    )

    # 9. Consensus Meta-Reviewer emits certified exit synthesis
    ledger.append(
        "artifact_accepted",
        effect="accepted",
        seat="meta_review",
        authority="may_emit_exit_artifact",
        payload={
            "subject": "tournament_final_report",
            "selected_lead_hypothesis": "H1",
            "lead_title": "G-quadruplex RNA stabilization selectively halts viral translation",
            "experimental_protocol_recommendations": [
                "Biophysical circular dichroism assay",
                "Luciferase reporter translation assay",
            ],
        },
    )

    # 10. Round Assessor verifies all requirements and legal close
    closed_legally = True
    ledger.append(
        "closed",
        effect="accepted",
        seat="round_assessor",
        authority="may_assess_round",
        payload={"subject": "tournament_final_report", "closed_legally": True},
    )
    ledger.append("run_finished", payload={"closed_legally": True})

    # 11. Verify cryptographic hash chain
    chain = verify_chain(ledger_path)
    assert chain.ok, f"Chain validation failed: {chain.problems}"

    # 12. Export HTML view
    view_path = export_html_view(ledger_path, out_dir / "tournament_trajectory.html")

    return {
        "status": "completed",
        "lead_hypothesis": "H1",
        "closed_legally": closed_legally,
        "ledger_length": chain.length,
        "ledger_head": chain.head,
        "view_html": str(view_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Autonomous Research Lab Tournament reference deployment"
    )
    parser.add_argument("--out", default="./runs/research_tournament_demo", help="Output directory")
    args = parser.parse_args()

    result = run_research_tournament(args.out)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
