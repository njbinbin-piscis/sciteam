"""Round-driven self-evolution protocol (docs/70).

A source stream is processed one task per round. Each round starts from a frozen snapshot of the
improvements promoted so far (transient state is not part of it), runs the task, may propose one
update, and the update enters the next snapshot only through the gate. Held-out (same
distribution) and ood tasks are scored with the first and the last snapshot; ood never produces
or selects an update. Everything that touches a model or a sandbox is a callback, so the driver
is deterministic and testable.

  runner(task, snapshot)            -> {"score": float, "used": [entry ids], "cost": float}
  propose(task, result, snapshot)   -> Update | None
  replay(update, tasks, snapshot)   -> {"reproduced": bool, "used": bool}: the update, applied
                                       to the replay set, reproduces the repair and is used
  score(task, snapshot)             -> float  used for validation, held-out and ood
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILLS, RULES = "skill", "rule"


@dataclass(frozen=True)
class Arm:
    name: str
    kinds: tuple[str, ...]          # which update kinds may be promoted; () = frozen
    linked: bool = False            # skill and rule updates are promoted together


ARMS = {
    "frozen": Arm("frozen", ()),
    "skills": Arm("skills", (SKILLS,)),
    "rules": Arm("rules", (RULES,)),
    "linked": Arm("linked", (SKILLS, RULES), linked=True),
}


@dataclass(frozen=True)
class Update:
    id: str
    kind: str                       # "skill" | "rule"
    files: dict[str, str]           # path -> content, the whole update


@dataclass
class GateConfig:
    """Switches double as ablations; each is recorded in the ledger."""
    replay: bool = True             # the update must reproduce the repair on the replay set
    usage: bool = True              # the replay run must actually use the new component
    validation: bool = True         # at least min_improved better and at most max_regressed worse
    min_improved: int = 2
    max_regressed: int = 1
    tolerance: float = 1e-9


def snapshot_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def gate(update: Update, *, replay: Callable[[], dict[str, Any]],
         before: Callable[[], list[float]], after: Callable[[], list[float]],
         cfg: GateConfig) -> tuple[bool, str]:
    """Promote only when every enabled check passes; the reason names the first that fails."""
    if cfg.replay or cfg.usage:
        rep = replay()
        if cfg.replay and not rep.get("reproduced"):
            return False, "replay_not_reproduced"
        if cfg.usage and not rep.get("used"):
            return False, "not_used"
    if cfg.validation:
        b, a = before(), after()
        improved = sum(1 for x, y in zip(b, a, strict=True) if y > x + cfg.tolerance)
        regressed = sum(1 for x, y in zip(b, a, strict=True) if y < x - cfg.tolerance)
        if improved < cfg.min_improved:
            return False, "validation_not_improved"
        if regressed > cfg.max_regressed:
            return False, "validation_regressed"
    return True, "promoted"


@dataclass
class RoundCallbacks:
    runner: Callable[[str, dict[str, str]], dict[str, Any]]
    propose: Callable[[str, dict[str, Any], dict[str, str]], Update | None]
    replay: Callable[[Update, list[str], dict[str, str]], dict[str, Any]]
    score: Callable[[str, dict[str, str]], float]


@dataclass
class ProtocolResult:
    arm: str
    order_seed: int
    ledger: list[dict[str, Any]] = field(default_factory=list)
    final_snapshot: dict[str, str] = field(default_factory=dict)
    heldout_first: dict[str, float] = field(default_factory=dict)
    heldout_last: dict[str, float] = field(default_factory=dict)
    ood_first: dict[str, float] = field(default_factory=dict)
    ood_last: dict[str, float] = field(default_factory=dict)
    source_scores: dict[str, float] = field(default_factory=dict)
    replay_before: dict[str, float] = field(default_factory=dict)
    replay_after: dict[str, float] = field(default_factory=dict)
    cost: float = 0.0


def source_order(source: list[str], seed: int) -> list[str]:
    o = list(source)
    random.Random(seed).shuffle(o)
    return o


def asdict_cfg(cfg: GateConfig) -> dict[str, Any]:
    return dict(cfg.__dict__)


def run_protocol(splits: dict[str, Any], arm: Arm, order_seed: int, cb: RoundCallbacks,
                 cfg: GateConfig | None = None, out: Path | None = None) -> ProtocolResult:
    cfg = cfg or GateConfig()
    snap: dict[str, str] = {}
    res = ProtocolResult(arm.name, order_seed)
    val, replay_set = splits["validation"], splits["replay"]
    res.heldout_first = {t: cb.score(t, snap) for t in splits["heldout"]}
    res.ood_first = {t: cb.score(t, snap) for t in splits["ood"]}
    prev = snapshot_hash(snap)
    for k, task in enumerate(source_order(splits["source"], order_seed), start=1):
        r = cb.runner(task, dict(snap))                      # the run sees a copy: no leakage
        res.cost += float(r.get("cost") or 0.0)
        res.source_scores[task] = float(r["score"])
        if task in replay_set:
            res.replay_before.setdefault(task, float(r["score"]))
        entry: dict[str, Any] = {"round": k, "task": task, "score": r["score"],
                                 "snapshot": prev, "gate": asdict_cfg(cfg), "decision": "none"}
        upd = cb.propose(task, r, dict(snap)) if arm.kinds else None
        if upd is not None and upd.kind in arm.kinds:
            cand = {**snap, **upd.files}
            ok, why = gate(
                upd, cfg=cfg,
                replay=lambda u=upd, s=dict(snap): cb.replay(u, replay_set, s),
                before=lambda s=dict(snap): [cb.score(t, s) for t in val],
                after=lambda c=cand: [cb.score(t, c) for t in val])
            entry.update(update=upd.id, kind=upd.kind, decision="promoted" if ok else "rejected",
                         reason=why)
            if ok:
                snap = cand
        prev = snapshot_hash(snap)
        entry["snapshot_after"] = prev
        res.ledger.append(entry)
    res.final_snapshot = snap
    res.replay_after = {t: cb.score(t, snap) for t in replay_set}
    res.heldout_last = {t: cb.score(t, snap) for t in splits["heldout"]}
    res.ood_last = {t: cb.score(t, snap) for t in splits["ood"]}
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{arm.name}-seed{order_seed}.json").write_text(
            json.dumps({**res.__dict__}, indent=1, sort_keys=True, default=str))
    return res
