"""Open research seed — any operator goal, no domain-locked frozen eval.

Thesis path for judges: the harness + institutions run a multi-team science
process on an arbitrary standing goal. Domain wet-lab claims that cannot be
machine-verified in-sandbox must be framed as external gaps (problem_frame),
not faked as success=true.
"""

from __future__ import annotations

from typing import Any


def seed_open_research_plan(
    *,
    campaign_id: str = "camp_open",
    goal: str = "",
) -> dict[str, Any]:
    g = (goal or "").strip() or ("Operator-supplied open research goal (domain-agnostic).")
    return {
        "mission_id": "m_plan_seed_open",
        "problem_choice": {
            "chosen_problem_id": "open_operator_goal",
            "rationale": (
                "Open profile: problem identity comes from the operator goal, "
                "not a pre-registered benchmark pack. The team must reframe the "
                "goal, declare sandbox-verifiable vs external-lab work, and keep "
                "narrative aligned with the ledger."
            ),
            "grounding_citation_keys": [],
            "scores": {"open_operator_goal": 1.0},
        },
        "hypotheses": [],
        "missions": [
            {
                "deps": [],
                "on_fail": {"action": "retry", "max_times": 2},
                "mission": {
                    "id": "m_protocol",
                    "kind": "protocol",
                    "goal": (
                        "Register an open-research protocol: operator goal is "
                        f"authoritative; no domain frozen eval is assumed. Goal: {g[:400]}"
                    ),
                    "paradigm": "pipeline_chain",
                    "exit_contract": "protocol_registration",
                    "metadata": {"runtime": "harness", "artifact_source": "harness"},
                    "skills_allowlist": ["protocol.register"],
                },
            },
            {
                "deps": ["m_protocol"],
                "on_fail": {"action": "retry", "max_times": 2},
                "mission": {
                    "id": "m_rules",
                    "kind": "legislation",
                    "goal": (
                        "Enact campaign_rules under GRUNDNORM for open research: "
                        "no fabricated wet-lab results; mark unverifiable claims; "
                        "honest negative / partial packaging allowed."
                    ),
                    "paradigm": "planning_council",
                    "exit_contract": "campaign_rules",
                    # Legislation runs on the LLM council, not the scripted worker:
                    # the campaign's own rules are enacted by agents.
                    "skills_allowlist": ["rules.legislate"],
                    "metadata": {
                        "artifact_gate": {
                            "pointer": "/grundnorm_ref",
                            "equals": "assets/institutions/GRUNDNORM.md",
                        }
                    },
                },
            },
            {
                "deps": ["m_rules"],
                "on_fail": {"action": "retry", "max_times": 2},
                "mission": {
                    "id": "m_frame",
                    "kind": "problem_frame",
                    "goal": (
                        "Reframe the operator goal into a research problem frame: "
                        "domain, success criteria, what this sandbox can verify, "
                        "what requires external labs (e.g. field breeding), and an "
                        "honesty note. Operator goal:\n"
                        f"{g}"
                    ),
                    "paradigm": "planning_council",
                    "exit_contract": "problem_frame",
                    "skills_allowlist": [
                        "hypothesis.formulate",
                        "lit.citation_guard",
                        "budget.report",
                    ],
                    "metadata": {
                        "artifact_gate": {
                            "pointer": "/mission_id",
                            "equals": "m_frame",
                        }
                    },
                },
            },
            {
                "deps": ["m_frame"],
                "on_fail": {"action": "retry", "max_times": 2},
                "mission": {
                    "id": "m_scout",
                    "kind": "literature",
                    "goal": (
                        "Survey literature relevant to the framed problem. Prefer "
                        "real sources; mark uncertain citations. Operator goal:\n"
                        f"{g[:500]}"
                    ),
                    "paradigm": "survey_gather",
                    "exit_contract": "lit_map",
                    "skills_allowlist": [
                        "lit.survey",
                        "lit.extract_claims",
                        "lit.citation_guard",
                    ],
                    "metadata": {
                        # 2026-08-14 audit finding (A3/A9/A11): institutions.py's
                        # audit_matrix has always declared lit_map/candidate_hypotheses/
                        # method_plan/attack_report/manuscript_checklist as requiring a
                        # "citation" audit, but no open-research mission ever ran one —
                        # mission.py's fail-closed `metadata.artifact_audit` mechanism
                        # is already proven end-to-end (run_paradigm_matrix_live.py,
                        # tests/test_artifact_audit_coverage.py); reusing the same
                        # proven pointer here instead of leaving it unwired.
                        "artifact_audit": {"tool": "citation_audit", "pointer": "/entries"},
                    },
                },
            },
            {
                "deps": ["m_scout"],
                "on_fail": {"action": "retry", "max_times": 2},
                "mission": {
                    "id": "m_hyp",
                    "kind": "hypothesis",
                    "goal": (
                        "Propose 1–3 falsifiable hypotheses for the framed problem. "
                        "Each must state how it could be falsified (sandbox proxy or "
                        "external lab). Do not invent experimental numbers."
                    ),
                    "paradigm": "debate_elo",
                    "exit_contract": "candidate_hypotheses",
                    "skills_allowlist": [
                        "hypothesis.formulate",
                        "hypothesis.refine",
                        "lit.citation_guard",
                    ],
                    "metadata": {
                        # 2026-08-14 audit finding (A3/A9/A11) — see m_scout above.
                        "artifact_audit": {"tool": "citation_audit", "pointer": "/candidates"},
                    },
                },
            },
            {
                "deps": ["m_hyp"],
                "on_fail": {"action": "retry", "max_times": 1},
                "mission": {
                    "id": "m_method",
                    "kind": "method",
                    "goal": (
                        "Produce a method/experimental plan implementing the top "
                        "hypothesis. Mark which steps are sandbox-executable vs "
                        "external. Do not claim wet-lab completion inside the sandbox."
                    ),
                    "paradigm": "pipeline_chain",
                    "exit_contract": "method_plan",
                    "skills_allowlist": [
                        "design.algorithm",
                        "design.ablation_plan",
                        "paper.method",
                    ],
                    "metadata": {
                        # 2026-08-14 audit finding (A3/A9/A11) — see m_scout above.
                        "artifact_audit": {"tool": "citation_audit", "pointer": "/citations"},
                    },
                },
            },
            {
                "deps": ["m_method"],
                "on_fail": {"action": "continue"},
                "mission": {
                    "id": "m_stress",
                    "kind": "adversarial",
                    "goal": (
                        "Attack the frame, hypotheses, and method: overclaim risk, "
                        "missing controls, sandbox/external conflation."
                    ),
                    "paradigm": "adversarial_pair",
                    "exit_contract": "attack_report",
                    "skills_allowlist": ["adversary.attack", "lit.citation_guard"],
                    "metadata": {
                        # 2026-08-14 audit finding (A3/A9/A11) — see m_scout above.
                        "artifact_audit": {"tool": "citation_audit", "pointer": "/citations"},
                    },
                },
            },
            {
                "deps": ["m_stress"],
                "on_fail": {"action": "retry", "max_times": 1},
                "mission": {
                    "id": "m_write",
                    "kind": "write",
                    "goal": (
                        "Write an honest research report. Separate sandbox-supported "
                        "claims from external-lab requirements. No fabricated yields "
                        "or field trial numbers."
                    ),
                    "paradigm": "write_atelier",
                    "exit_contract": "manuscript_checklist",
                    "skills_allowlist": [
                        "paper.outline",
                        "paper.related_work",
                        "paper.method",
                        "paper.checklist",
                    ],
                    "metadata": {
                        # 2026-08-14 audit finding (A3/A9/A11) — see m_scout above.
                        "artifact_audit": {
                            "tool": "citation_audit",
                            "pointer": "/citation_audit/entries",
                        },
                    },
                },
            },
            {
                "deps": ["m_write"],
                "on_fail": {"action": "retry", "max_times": 2},
                "mission": {
                    "id": "m_package",
                    "kind": "package",
                    "goal": "Package evidence bundle (positive, partial, or honest-negative).",
                    "paradigm": "pipeline_chain",
                    "exit_contract": "package_manifest",
                    "metadata": {"runtime": "harness", "artifact_source": "harness"},
                    "skills_allowlist": ["package.arm"],
                },
            },
        ],
        "locked_ids": ["m_protocol", "m_rules", "m_package"],
        "risks": [
            "Operator goals that require wet-lab / field work cannot be fully closed in-sandbox.",
            "Without a plugged-in domain verifier pack, scientific 'win' is "
            "process+honesty, not yield metrics.",
        ],
        "fallback": "Package partial/honest-negative with explicit external-resource gaps.",
        "budget": {"max_revisions": 2, "max_missions": 16},
    }
