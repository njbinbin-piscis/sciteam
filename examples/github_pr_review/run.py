"""GitHub PR Review and Merge Control — Reference Institution Deployment.

Demonstrates how Institution Engineering replaces brittle prompt-based bots
with a mechanically enforceable, tamper-evident institutional control plane.

Workflow Steps:
  1. PR Author (implementer) emits PR patch.
  2. Security Scanner (red_team) detects a hardcoded secret in patch v1.
  3. Security Scanner issues an authorized VETO.
  4. Institutional planner mechanically REOPENS the author's work item.
  5. PR Author submits patch v2 reading credentials from environment.
  6. Code Quality Reviewer and Security Scanner both grant approvals.
  7. Release Assembler verifies the committee threshold and executes legal merge.
  8. All steps recorded in a hash-chained, auditable EventLog.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sciteam.event_log import LEDGER_FILENAME, EventLog, verify_chain
from sciteam.ledger_view import export_html_view
from sciteam.team_loader import load_team_config

PATCH_V1 = """\
# PR #42: Authentication module (v1)
import os

JWT_SECRET = "super-secret-key-12345"  # Security flaw: hardcoded secret!

def generate_token(user_id: str) -> str:
    return f"token_for_{user_id}_{hash(JWT_SECRET)}"
"""

PATCH_V2 = """\
# PR #42: Authentication module (v2 - repaired)
import os

JWT_SECRET = os.environ.get("JWT_SECRET_KEY")
if not JWT_SECRET:
    raise ValueError("JWT_SECRET_KEY environment variable required")

def generate_token(user_id: str) -> str:
    return f"token_for_{user_id}_{hash(JWT_SECRET)}"
"""


def run_pr_review_institution(output_dir: Path | str) -> dict[str, Any]:
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = Path(__file__).parent / "institution.yaml"
    team_cfg = load_team_config(manifest_path)

    ledger_path = out_dir / LEDGER_FILENAME
    ledger = EventLog(ledger_path, run_id=f"pr_review_{out_dir.name}")

    # 1. Run start and rule version
    ledger.append("run_started", payload={"pr_number": 42, "team": team_cfg.team_id})
    ledger.set_rule_version("rule@github_pr_v1.0")

    # 2. Unauthorized bystander attempt (e.g. random external bot tries to approve)
    ledger.append(
        "artifact_submitted",
        effect="rejected",
        seat="external_bot",
        authority="may_approve",
        payload={"subject": "pr_42", "reason": "No standing in institution roster"},
    )

    # 3. PR Author submits patch v1
    workspace = out_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "auth.py").write_text(PATCH_V1, encoding="utf-8")

    ledger.append(
        "artifact_accepted",
        effect="accepted",
        seat="implementer",
        authority="may_propose_patch",
        payload={"subject": "pr_patch_v1", "files": ["auth.py"]},
    )

    # 4. Code Quality Reviewer passes logic and syntax
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="reviewer",
        authority="may_review_code",
        payload={"subject": "pr_patch_v1", "quality_score": 0.95, "passed": True},
    )

    # 5. Security Scanner audits and catches hardcoded secret
    ledger.append(
        "veto_issued",
        effect="accepted",
        seat="red_team",
        authority="may_veto",
        payload={
            "subject": "pr_patch_v1",
            "reason": "security_hardcoded_secret",
            "detail": "auth.py contains plaintext JWT_SECRET",
        },
    )

    # 6. Institutional planner reopens patch item
    ledger.append(
        "reopened",
        effect="accepted",
        seat="planner",
        authority="may_revise_plan",
        payload={"subject": "pr_patch_v1", "reason": "authorized security veto"},
    )

    # 7. PR Author repairs code and submits patch v2
    (workspace / "auth.py").write_text(PATCH_V2, encoding="utf-8")
    ledger.append(
        "artifact_accepted",
        effect="accepted",
        seat="implementer",
        authority="may_propose_patch",
        payload={"subject": "pr_patch_v2", "files": ["auth.py"], "repaired": True},
    )

    # 8. Both reviewers re-audit and pass patch v2
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="reviewer",
        authority="may_review_code",
        payload={"subject": "pr_patch_v2", "quality_score": 0.98, "passed": True},
    )
    ledger.append(
        "audit_reported",
        effect="accepted",
        seat="red_team",
        authority="may_veto",
        payload={"subject": "pr_patch_v2", "vulnerabilities_found": 0, "passed": True},
    )

    # 9. Release Assembler verifies 2/2 committee approvals and legally merges
    approvals = {"reviewer": True, "red_team": True}
    closed_legally = all(approvals.values()) and len(approvals) >= 2

    ledger.append(
        "closed",
        effect="accepted" if closed_legally else "rejected",
        seat="assembler",
        authority="may_close",
        payload={"subject": "pr_42", "approvals": approvals, "merged": closed_legally},
    )
    ledger.append("run_finished", payload={"closed_legally": closed_legally})

    # 10. Verify cryptographic hash chain
    chain = verify_chain(ledger_path)
    assert chain.ok, f"Chain validation failed: {chain.problems}"

    # 11. Generate standalone visual HTML trajectory
    view_path = export_html_view(ledger_path, out_dir / "pr_review_trajectory.html")

    return {
        "pr_number": 42,
        "closed_legally": closed_legally,
        "ledger_length": chain.length,
        "ledger_head": chain.head,
        "view_html": str(view_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run GitHub PR review institution reference deployment"
    )
    parser.add_argument("--out", default="./runs/github_pr_review_demo", help="Output directory")
    args = parser.parse_args()

    result = run_pr_review_institution(args.out)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
