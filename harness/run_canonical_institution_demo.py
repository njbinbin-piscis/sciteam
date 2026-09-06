#!/usr/bin/env python3
"""Canonical Institution Engineering demo (line A `run-canonical-demo`).

Runs a small, real repo-level bug-fix task through an explicit institution:
declared seats (implementer, auditor, chair; a bystander with no seat), where
a seat's output only gains system-level validity through mechanics this repo
already ships and unit-tests in isolation:

- authority ("who may write the exit artifact") — `sciteam.worker_common.is_artifact_role`,
  the exact predicate the live LLM worker enforces (`tests/test_llm_worker.py`);
- veto -> mechanical reopen — `sciteam.audit_veto.VetoUpstreamReviser` driving
  `sciteam.adaptive_planner.AdaptiveCampaignPlanner.reopen_upstream`
  (`tests/test_audit_veto.py`, `tests/test_reopen_upstream.py`);
- mechanical rollback — `sciteam.amendment.rollback_amendment`
  (`tests/test_amendment.py`).

Seats here are *scripted* (deterministic Python callables), not live LLM
calls — the zero-cost existence-proof tier this codebase always ships first
(cf. `harness/run_isa_hyp_tournament.py`'s fake stage before its live-LLM
follow-up, `INSTITUTION_ITER.md` row 3). A live-LLM canonical run is the
natural next funded step; per this project's own A10 discipline
(`paper/rebuildV2/02-design-requirements.md`: real budget requires a prior
verbatim-material review pass), that is out of scope for this run and is
tracked as follow-up, not silently skipped.

Modes (--mode):
    full           canonical run: unauthorized submission rejected, a buggy
                   first patch triggers an authorized veto -> mechanical
                   reopen, a corrected second patch passes, chair closes
                   legally. This is the run whose ledger the paper cites.
    prompt_only    ablation: no machine-checked test execution; the chair
                   accepts the implementer's own narrative "done" claim.
    no_separation  ablation: the implementer is also the auditor (no
                   separation of powers) — self-review only.
    no_recovery    ablation: a failed audit has no legal reopen path (no
                   veto mechanism wired at all).

--config-swap switches the institution manifest's closing rule from "one
auditor approves" to "two-auditor committee, both must approve" — only the
manifest changes; none of the mechanics above are touched (sciteam/*.py is
byte-identical across --config-swap runs, checked and recorded as `e0_diff`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from sciteam import audit_veto  # noqa: E402
from sciteam.adaptive_planner import AdaptiveCampaignPlanner, CompositeReviser  # noqa: E402
from sciteam.campaign import BudgetClock, CampaignBudget  # noqa: E402
from sciteam.kb import CampaignKB  # noqa: E402
from sciteam.mission import MissionOutcome  # noqa: E402
from sciteam.worker_common import is_artifact_role  # noqa: E402

INTERPRETER_FILES = (
    "sciteam/amendment.py",
    "sciteam/audit_veto.py",
    "sciteam/adaptive_planner.py",
    "sciteam/behavioral_shadow.py",
    "sciteam/worker_common.py",
    "harness/run_canonical_institution_demo.py",
)

# --- the repo-fix task ------------------------------------------------------

_BUGGY_CALC = '''\
def apply_discount(price: float, pct: float) -> float:
    """Return price after applying a percentage discount.

    Known bug (this is the task): does not clamp `pct`, so a caller passing
    pct > 100 produces a negative price instead of a floor at 0.
    """
    return price * (1 - pct / 100)
'''

_FIXED_CALC = '''\
def apply_discount(price: float, pct: float) -> float:
    """Return price after applying a percentage discount, floored at 0."""
    pct = max(0.0, min(100.0, pct))
    return price * (1 - pct / 100)
'''

_FROZEN_TEST = """\
from calc import apply_discount


def test_normal_discount():
    assert apply_discount(100.0, 25) == 75.0


def test_discount_over_100_floors_at_zero():
    assert apply_discount(100.0, 150) == 0.0


def test_zero_discount_is_noop():
    assert apply_discount(50.0, 0) == 50.0
"""


def _write_task(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "calc.py").write_text(_BUGGY_CALC, encoding="utf-8")
    (workspace / "test_calc.py").write_text(_FROZEN_TEST, encoding="utf-8")


def _run_frozen_tests(workspace: Path) -> dict[str, Any]:
    """Verifier-owned: only this function's subprocess result decides pass/fail —
    no seat, authorized or not, may declare tests green by narration alone."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "passed": proc.returncode == 0,
    }


def _run_self_syntax_check(workspace: Path) -> dict[str, Any]:
    """Shallow self-check standing in for "author reviews own work": confirms
    the file still parses, never runs the frozen behavioral test suite. This
    is deliberately weaker than `_run_frozen_tests`, not merely "the same
    check run by a different seat" — the risk `no_separation` demonstrates is
    that separation of powers is what institutionally *forces* the objective
    check to happen at all, not that the same seat name computes something
    different."""
    import ast

    try:
        ast.parse((workspace / "calc.py").read_text(encoding="utf-8"))
        return {"passed": True, "check": "syntax_only"}
    except SyntaxError as exc:
        return {"passed": False, "check": "syntax_only", "error": str(exc)}


# --- institution manifest ---------------------------------------------------


def _manifest(config_swap: bool) -> dict[str, Any]:
    if config_swap:
        return {
            "emit_roles": ["implementer"],
            "may_veto_roles": ["auditor_1", "auditor_2"],
            "approval_rule": {
                "kind": "committee",
                "roles": ["auditor_1", "auditor_2"],
                "threshold": 2,
            },
        }
    return {
        "emit_roles": ["implementer"],
        "may_veto_roles": ["auditor"],
        "approval_rule": {"kind": "single", "roles": ["auditor"], "threshold": 1},
    }


# --- scripted seats ----------------------------------------------------------


def _node(node_id: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "deps": list(deps or []),
        "mission": {
            "id": node_id,
            "goal": f"run {node_id}",
            "paradigm": "pipeline_chain",
            "exit_contract": "results",
        },
    }


def _outcome(node_id: str, *, succeeded: bool = True) -> MissionOutcome:
    return MissionOutcome(
        mission_id=node_id,
        status="completed" if succeeded else "failed",
        team_run_id="canonical_demo",
        rounds_used=1,
        wall_clock_seconds=0.01,
        contract_ok=succeeded,
        artifact_path="",
    )


def _implementer_propose(
    workspace: Path, attempt: int, manifest: dict[str, Any], log: list[str]
) -> None:
    content = _BUGGY_CALC if attempt == 1 else _FIXED_CALC
    (workspace / "calc.py").write_text(content, encoding="utf-8")
    granted = is_artifact_role(
        "implementer", {"artifact_emit_roles": manifest["emit_roles"], "assess_roles": []}
    )
    assert granted, "canonical run precondition: implementer must hold artifact-duty standing"
    log.append(
        f"implementer(attempt={attempt}): wrote patch, submission authorized (seat=implementer)"
    )


def _bystander_attempt_submit(manifest: dict[str, Any], log: list[str]) -> bool:
    """An agent with no declared seat tries to make its output count.
    Returns whether the submission was granted institutional effect."""
    granted = is_artifact_role(
        "bystander", {"artifact_emit_roles": manifest["emit_roles"], "assess_roles": []}
    )
    log.append(f"bystander: attempted submission, authorized={granted} (expected: False)")
    return granted


def _auditor_review(
    workspace: Path,
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    auditor_key: str,
    attempt: int,
    log: list[str],
) -> dict[str, Any]:
    result = _run_frozen_tests(workspace)
    log.append(f"{auditor_key}(attempt={attempt}): frozen tests passed={result['passed']}")
    if not result["passed"]:
        audit_veto.write_veto(
            run_dir / "missions" / f"m_audit_{auditor_key}_{attempt}",
            target="m_implement",
            reason_code="late_test_failure",
            vetoed_by=auditor_key,
            vetoed_at=f"2026-09-06T00:{attempt:02d}:00+00:00",
            detail="frozen test_calc.py failed against submitted patch",
        )
        log.append(f"{auditor_key}: wrote authorized veto targeting m_implement")
    return result


def _chair_close(
    approvals: dict[str, bool],
    manifest: dict[str, Any],
    test_result: dict[str, Any],
    log: list[str],
) -> dict[str, Any]:
    rule = manifest["approval_rule"]
    granted = sum(1 for k in rule["roles"] if approvals.get(k))
    closed = granted >= int(rule["threshold"])
    verdict = {
        "closed_legally": closed,
        "approval_rule": rule,
        "approvals": approvals,
        "tests_passed": test_result.get("passed"),
    }
    log.append(f"chair: closed_legally={closed} (approvals={approvals}, rule={rule})")
    return verdict


# --- run modes ----------------------------------------------------------------


def run_full(out_dir: Path, *, config_swap: bool, log: list[str]) -> dict[str, Any]:
    manifest = _manifest(config_swap)
    workspace = out_dir / "workspace"
    _write_task(workspace)
    run_dir = out_dir  # missions/ scanned relative to this

    reviser = audit_veto.VetoUpstreamReviser(
        run_dir=run_dir, authorized_seats=frozenset(manifest["may_veto_roles"])
    )
    planner = AdaptiveCampaignPlanner(
        {"missions": [_node("m_implement"), _node("m_audit", ["m_implement"])]},
        reviser=CompositeReviser([reviser]),
        max_revisions=3,
    )
    kb = CampaignKB(out_dir / "kb.jsonl")
    clock = BudgetClock(CampaignBudget())

    _bystander_attempt_submit(manifest, log)

    attempt = 1
    auditor_keys = manifest["approval_rule"]["roles"]
    approvals: dict[str, bool] = dict.fromkeys(auditor_keys, False)
    last_test_result: dict[str, Any] = {}
    for _tick in range(12):  # hard tick cap: this DAG never needs more than a handful
        decision = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
        if decision.action != "run_mission" or decision.mission is None:
            break
        base_id = (
            decision.mission.id.rsplit("_a", 1)[0]
            if "_a" in decision.mission.id
            else decision.mission.id
        )
        if base_id == "m_implement":
            _implementer_propose(workspace, attempt, manifest, log)
            planner.observe(_outcome(decision.mission.id, succeeded=True))
        elif base_id == "m_audit":
            approvals = dict.fromkeys(auditor_keys, False)
            for auditor_key in auditor_keys:
                last_test_result = _auditor_review(
                    workspace, run_dir, manifest, auditor_key=auditor_key, attempt=attempt, log=log
                )
                approvals[auditor_key] = last_test_result["passed"]
            attempt += 1
            planner.observe(_outcome(decision.mission.id, succeeded=True))
        else:  # pragma: no cover — DAG only has the two nodes above
            planner.observe(_outcome(decision.mission.id, succeeded=True))

    verdict = _chair_close(approvals, manifest, last_test_result, log)
    return {
        "manifest": manifest,
        "verdict": verdict,
        "plan_snapshot": planner.plan_snapshot(),
        "final_calc_py": (workspace / "calc.py").read_text(encoding="utf-8"),
    }


def run_prompt_only(out_dir: Path, *, log: list[str]) -> dict[str, Any]:
    """Ablation: chair trusts the implementer's own narrative, never runs pytest."""
    manifest = _manifest(config_swap=False)
    workspace = out_dir / "workspace"
    _write_task(workspace)
    _implementer_propose(
        workspace, attempt=1, manifest=manifest, log=log
    )  # buggy patch, attempt 1 only
    narrative_claim = "implementer narrates: tests pass, fix is complete"
    log.append(f"auditor(prompt_only): {narrative_claim} (no subprocess run)")
    approvals = {"auditor": True}  # accepted on narration alone
    verdict = _chair_close(approvals, manifest, {"passed": None}, log)
    real_result = _run_frozen_tests(workspace)  # recorded for the paper, not consulted by the chair
    return {
        "manifest": manifest,
        "verdict": verdict,
        "ground_truth_tests_actually_passed": real_result["passed"],
        "final_calc_py": (workspace / "calc.py").read_text(encoding="utf-8"),
    }


def run_no_separation(out_dir: Path, *, log: list[str]) -> dict[str, Any]:
    """Ablation: implementer also holds auditor authority (self-review).

    The failure mode this demonstrates is not "the same check computes a
    different answer" — it is that separation of powers is what
    institutionally *forces* the objective frozen-test check to run at all.
    With the implementer also seated as auditor, nothing in the institution
    requires anyone independent to invoke it, so the self-review here only
    does a shallow syntax check (see `_run_self_syntax_check`) — which the
    still-buggy patch passes."""
    manifest = _manifest(config_swap=False)
    manifest["may_veto_roles"] = ["implementer"]
    manifest["approval_rule"] = {"kind": "single", "roles": ["implementer"], "threshold": 1}
    workspace = out_dir / "workspace"
    _write_task(workspace)
    _implementer_propose(
        workspace, attempt=1, manifest=manifest, log=log
    )  # buggy patch, attempt 1 only
    self_check = _run_self_syntax_check(workspace)
    log.append(f"implementer-as-auditor: self-check(syntax only) passed={self_check['passed']}")
    ground_truth = _run_frozen_tests(
        workspace
    )  # recorded for the paper, not consulted institutionally
    approvals = {"implementer": self_check["passed"]}
    verdict = _chair_close(approvals, manifest, self_check, log)
    return {
        "manifest": manifest,
        "verdict": verdict,
        "ground_truth_tests_actually_passed": ground_truth["passed"],
        "final_calc_py": (workspace / "calc.py").read_text(encoding="utf-8"),
    }


def run_no_recovery(out_dir: Path, *, log: list[str]) -> dict[str, Any]:
    """Ablation: a failed audit has no legal reopen path (no reviser wired)."""
    manifest = _manifest(config_swap=False)
    workspace = out_dir / "workspace"
    _write_task(workspace)
    planner = AdaptiveCampaignPlanner(
        {"missions": [_node("m_implement"), _node("m_audit", ["m_implement"])]},
        reviser=None,  # <- the ablation: no veto->reopen mechanism at all
        max_revisions=0,
    )
    kb = CampaignKB(out_dir / "kb.jsonl")
    clock = BudgetClock(CampaignBudget())
    first = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    _implementer_propose(workspace, attempt=1, manifest=manifest, log=log)
    planner.observe(_outcome(first.mission.id, succeeded=True))  # type: ignore[union-attr]
    second = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    result = _auditor_review(
        workspace, out_dir, manifest, auditor_key="auditor", attempt=1, log=log
    )
    planner.observe(_outcome(second.mission.id, succeeded=True))  # type: ignore[union-attr]
    # No reopen mechanism exists to act on the veto file just written: the
    # campaign has no legal move left even though a correct fix (attempt 2)
    # was one implementer turn away.
    third = planner.next_mission(last_outcome=None, kb=kb, clock=clock)
    stuck = third.mission is not None and third.mission.id == first.mission.id  # type: ignore[union-attr]
    next_desc = "same node again, no fix applied" if stuck else third.action
    log.append(f"no_recovery: audit failed and vetoed, but planner offers next={next_desc}")
    verdict = {
        "closed_legally": False,
        "reason": "no_legal_reopen_path",
        "tests_passed": result["passed"],
    }
    return {
        "manifest": manifest,
        "verdict": verdict,
        "final_calc_py": (workspace / "calc.py").read_text(encoding="utf-8"),
    }


def _interpreter_hash() -> str:
    digest = hashlib.sha256()
    for rel in INTERPRETER_FILES:
        path = LAB_ROOT / rel
        digest.update(rel.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode", choices=["full", "prompt_only", "no_separation", "no_recovery"], default="full"
    )
    parser.add_argument(
        "--config-swap", action="store_true", help="single-approver -> committee (only --mode full)"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output dir (default: runs/canonical_institution_demo/<mode>[_swap])",
    )
    args = parser.parse_args()

    name = args.mode + ("_swap" if args.config_swap else "")
    out_dir = (
        Path(args.out) if args.out else LAB_ROOT / "runs" / "canonical_institution_demo" / name
    )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    hash_before = _interpreter_hash()
    log: list[str] = []
    if args.mode == "full":
        result = run_full(out_dir, config_swap=args.config_swap, log=log)
    elif args.mode == "prompt_only":
        result = run_prompt_only(out_dir, log=log)
    elif args.mode == "no_separation":
        result = run_no_separation(out_dir, log=log)
    else:
        result = run_no_recovery(out_dir, log=log)
    hash_after = _interpreter_hash()

    summary = {
        "mode": args.mode,
        "config_swap": args.config_swap,
        "log": log,
        "interpreter_hash_before": hash_before,
        "interpreter_hash_after": hash_after,
        "interpreter_hash_unchanged": hash_before == hash_after,
        **result,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"plan_snapshot", "final_calc_py"}},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
