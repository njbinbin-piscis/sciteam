"""Behavioral shadow evaluation (line A, Institution Engineering close-loop gap #1).

Structural shadow (``scripts/run_shadow_eval.py``) proves the amended asset
tree still parses, every seat still resolves to a catalog role, and the
Grundnorm files are byte-identical. It cannot show whether the amendment
changes what the institution actually *does* — that requires exercising a
decision function against the amended text, not just loading it.

This module adds exactly one behavioral check, deliberately narrow and
mechanically decidable rather than judged by an LLM: for every paradigm
under the (incumbent / amended) asset tree, it evaluates the same authority
predicate the live worker enforces at submission time
(``sciteam.worker_common.is_artifact_role``) against every declared seat.
A "pass" means the institution text, as amended, still (a) grants at least
one seat standing to submit the exit artifact and (b) denies every seat not
declared as artifact-duty. Both directions matter: an amendment that widens
``artifact_emit_roles`` to everyone is as much a governance failure as one
that leaves no seat able to ever submit.

This is deliberately not a full live/scripted mission run. A mission run
through ``TeamOrchestrator`` + a fake runtime would still not exercise
``llm_worker.py``'s authority gate (the fake runtime never calls it), so it
would buy no more behavioral coverage than this direct predicate check while
costing an order of magnitude more code and runtime. If a future amendment
class needs a genuinely task-behavioral anchor (e.g. "does a changed
approval threshold change who can enact"), add a second, narrowly-scoped
anchor rather than generalizing this one prematurely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sciteam.team_loader import TeamConfig, load_team_configs
from sciteam.worker_common import is_artifact_role


def _paradigm_authority_result(cfg: TeamConfig) -> dict[str, Any]:
    emit_roles = cfg.artifact_emit_roles()
    assess_roles = cfg.assess_roles()
    meta = {"artifact_emit_roles": emit_roles, "assess_roles": assess_roles}

    granted = [m.agent_key for m in cfg.members if is_artifact_role(m.agent_key, meta)]
    non_duty = [
        m.agent_key
        for m in cfg.members
        if m.agent_key not in emit_roles and (m.role or m.agent_key) not in emit_roles
    ]
    wrongly_granted = [key for key in non_duty if is_artifact_role(key, meta)]

    has_standing_seat = bool(emit_roles) and bool(granted)
    no_privilege_escalation = not wrongly_granted
    ok = has_standing_seat and no_privilege_escalation

    return {
        "paradigm": cfg.team_id,
        "emit_roles": emit_roles,
        "granted": granted,
        "wrongly_granted": wrongly_granted,
        "has_standing_seat": has_standing_seat,
        "no_privilege_escalation": no_privilege_escalation,
        "ok": ok,
    }


def evaluate_authority_behavior(assets_root: Path | str) -> dict[str, Any]:
    """Mechanically decide submission authority for every paradigm.

    Returns ``verdict: "pass" | "fail" | "indeterminate"``. ``indeterminate``
    only when the asset tree has zero paradigms to evaluate (an amendment
    outside ``teams/paradigms/`` and unrelated institution files) — it must
    never be silently treated as "pass" by a caller.
    """
    paradigms_dir = Path(assets_root) / "teams" / "paradigms"
    try:
        configs = load_team_configs(paradigms_dir)
    except Exception as exc:  # noqa: BLE001 — a malformed amended tree is a fail, not a crash
        return {"paradigms": [], "verdict": "fail", "error": str(exc)}

    results = [_paradigm_authority_result(cfg) for cfg in configs]
    if not results:
        return {"paradigms": [], "verdict": "indeterminate"}
    verdict = "pass" if all(r["ok"] for r in results) else "fail"
    return {"paradigms": results, "verdict": verdict}


def run_behavioral_shadow(incumbent_root: Path | str, amended_root: Path | str) -> dict[str, Any]:
    """Compare authority behavior of the incumbent tree against the amended one.

    ``verdict`` is decided on the amended tree alone (that is the candidate
    under review); the incumbent result is retained for diff/provenance so a
    reader can see whether the amendment *introduced* the failure or the
    incumbent already had it (pre-existing failures are still reported, not
    hidden, but do not by themselves block promotion of an unrelated fix).
    """
    incumbent = evaluate_authority_behavior(incumbent_root)
    amended = evaluate_authority_behavior(amended_root)
    return {
        "incumbent": incumbent,
        "amended": amended,
        "verdict": amended["verdict"],
        "regression": amended["verdict"] == "fail" and incumbent["verdict"] != "fail",
    }
