"""Mutation testing for institutions: do your guards actually guard anything?

A manifest that loads cleanly proves nothing on its own. The question worth
answering is the inverse: **if someone quietly broke a rule, would the
system notice?** So this module deliberately corrupts an institution —
removes the authority that makes a veto possible, lets one seat both do the
work and judge it, duplicates a seat key so one silently shadows another —
and asks whether loading it still fails.

The vocabulary is standard mutation testing, mapped onto institutions:

* a **mutant** is a manifest with one rule broken;
* a mutant is **killed** if the loader rejects it;
* a **survivor** is a broken institution that loaded anyway — a real gap,
  and the only output here worth acting on.

One thing matters more than the score. Some mutations are **equivalent**:
they change the manifest without changing what it permits (reordering
seats, editing a description). A suite that kills those is not strict, it
is merely brittle — it would be "passing" even if it only asserted that any
edit at all is rejected. So equivalent mutants are declared up front and
**must survive**; killing one is reported as a failure of the same weight
as a survivor.

    sciteam mutate sciteam/data/assets/teams/paradigms/pipeline_chain.yaml

Operator names follow `docs/` M-numbers from the research programme's
operator table, so findings here map back to the criteria they threaten.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sciteam.team_loader import parse_team_config

Manifest = dict[str, Any]


@dataclass(frozen=True, slots=True)
class MutationOperator:
    """One way to break an institution, and what breaking it should prove."""

    name: str
    description: str
    apply: Callable[[Manifest], Manifest | None]
    #: False for *equivalent* mutants: changes that must NOT be rejected,
    #: because rejecting them would mean the loader is refusing edits rather
    #: than refusing illegitimate institutions.
    should_be_killed: bool = True
    #: What the mutation threatens, for mapping a survivor back to a rule.
    threatens: str = ""


@dataclass(frozen=True, slots=True)
class MutationResult:
    operator: str
    description: str
    applicable: bool
    should_be_killed: bool
    killed: bool
    threatens: str
    detail: str

    @property
    def ok(self) -> bool:
        """A result is fine when the outcome matched the expectation."""
        return not self.applicable or self.killed == self.should_be_killed


@dataclass(slots=True)
class MutationReport:
    source: str
    results: list[MutationResult] = field(default_factory=list)

    @property
    def applicable(self) -> list[MutationResult]:
        return [r for r in self.results if r.applicable]

    @property
    def survivors(self) -> list[MutationResult]:
        """Broken institutions the loader accepted."""
        return [r for r in self.applicable if r.should_be_killed and not r.killed]

    @property
    def false_kills(self) -> list[MutationResult]:
        """Harmless edits the loader rejected — brittleness, not strictness."""
        return [r for r in self.applicable if not r.should_be_killed and r.killed]

    @property
    def score(self) -> float:
        """Fraction of non-equivalent mutants killed. 1.0 means no survivors."""
        candidates = [r for r in self.applicable if r.should_be_killed]
        if not candidates:
            return 1.0
        return sum(1 for r in candidates if r.killed) / len(candidates)

    @property
    def ok(self) -> bool:
        return not self.survivors and not self.false_kills


# --- operators -------------------------------------------------------------------


def _emitters(manifest: Manifest) -> list[Manifest]:
    return [m for m in manifest.get("members", []) if m.get("emits_exit_artifact")]


def _assessors(manifest: Manifest) -> list[Manifest]:
    return [
        m for m in manifest.get("members", []) if "may_assess_round" in (m.get("authority") or [])
    ]


def _m2_merge_work_and_judgment(manifest: Manifest) -> Manifest | None:
    """Let the seat that judges the round also seal the artifact."""
    assessors = _assessors(manifest)
    if not assessors:
        return None
    assessors[0]["emits_exit_artifact"] = True
    return manifest


def _m3_drop_assessor_authority(manifest: Manifest) -> Manifest | None:
    """Remove the authority to judge while the institution still stops on judgment."""
    assessors = _assessors(manifest)
    if not assessors:
        return None
    for member in assessors:
        member["authority"] = [a for a in member["authority"] if a != "may_assess_round"]
        if not member["authority"]:
            member.pop("authority")
    return manifest


def _m8_alias_drift(manifest: Manifest) -> Manifest | None:
    """Misspell an authority: the seat looks authorized and holds nothing."""
    assessors = _assessors(manifest)
    if not assessors:
        return None
    member = assessors[0]
    member["authority"] = [
        "may_assess_rounds" if a == "may_assess_round" else a for a in member["authority"]
    ]
    return manifest


def _m7_duplicate_seat_key(manifest: Manifest) -> Manifest | None:
    """Two seats under one key — one silently shadows the other."""
    members = manifest.get("members") or []
    if len(members) < 2:
        return None
    members[1] = copy.deepcopy(members[1])
    members[1]["key"] = members[0]["key"]
    return manifest


def _m1_drop_emitter_flag(manifest: Manifest) -> Manifest | None:
    """Nobody may seal the exit artifact: the institution cannot close."""
    emitters = _emitters(manifest)
    if not emitters:
        return None
    for member in emitters:
        member.pop("emits_exit_artifact", None)
        member.pop("artifact_duty", None)
    return manifest


def _m15_erase_stopping_state(manifest: Manifest) -> Manifest | None:
    """Swap judgment for a generic stop, erasing the need for an assessor."""
    stopping = (manifest.get("coordination") or {}).get("stopping")
    if not isinstance(stopping, dict) or stopping.get("kind") != "judgment":
        return None
    stopping["kind"] = "verification"
    return manifest


def _m10_deaden_iterations(manifest: Manifest) -> Manifest | None:
    """A negative round ceiling, which normalizes to "unlimited".

    Note what this operator is *not*: setting ``max_iterations`` to 0 is
    legitimate — it means "no ceiling". Mutating it to 0 would be an
    equivalent mutant dressed up as a defect. The real hazard is a negative
    value, because clamping it to 0 turns a typo into the opposite of what
    it says.
    """
    stopping = (manifest.get("coordination") or {}).get("stopping")
    if not isinstance(stopping, dict):
        return None
    stopping["max_iterations"] = -1
    return manifest


def _m5_empty_roster(manifest: Manifest) -> Manifest | None:
    """An institution with no seats at all."""
    manifest["members"] = []
    return manifest


def _eq_reorder_members(manifest: Manifest) -> Manifest | None:
    members = manifest.get("members") or []
    if len(members) < 2:
        return None
    members.reverse()
    return manifest


def _eq_rewrite_prose(manifest: Manifest) -> Manifest | None:
    manifest["description"] = "Rewritten description with no institutional content."
    for member in manifest.get("members") or []:
        if member.get("name"):
            member["name"] = str(member["name"]).upper()
    return manifest


def _eq_add_unknown_key(manifest: Manifest) -> Manifest | None:
    manifest["x_editor_note"] = "annotation added by a tool"
    return manifest


#: Ordered so that the two equivalent mutants read last — they are the
#: control condition for the seven above them.
OPERATORS: tuple[MutationOperator, ...] = (
    MutationOperator(
        "M2/flip-separation",
        "the seat that judges the round also seals the exit artifact",
        _m2_merge_work_and_judgment,
        threatens="separation of powers (a seat cannot both produce and approve)",
    ),
    MutationOperator(
        "M3/skip-check",
        "remove may_assess_round while the institution still stops on judgment",
        _m3_drop_assessor_authority,
        threatens="judgment stopping requires someone authorized to judge",
    ),
    MutationOperator(
        "M8/alias-drift",
        "misspell an authority so a seat appears authorized but holds nothing",
        _m8_alias_drift,
        threatens="authority names are a closed vocabulary, not free text",
    ),
    MutationOperator(
        "M7/allow-overwrite",
        "duplicate a seat key so one member silently shadows another",
        _m7_duplicate_seat_key,
        threatens="seat identity is unique; shadowing makes attribution meaningless",
    ),
    MutationOperator(
        "M1/drop-flag",
        "remove the exit-artifact duty from every seat",
        _m1_drop_emitter_flag,
        threatens="some seat must be able to close the mission",
    ),
    MutationOperator(
        "M15/erase-state",
        "replace judgment stopping with verification, erasing the assessor requirement",
        _m15_erase_stopping_state,
        should_be_killed=False,
        threatens="",
    ),
    MutationOperator(
        "M10/deaden-branch",
        "set a negative round ceiling, which normalizes to unlimited",
        _m10_deaden_iterations,
        threatens="a nonsensical ceiling must fail, not silently become no ceiling",
    ),
    MutationOperator(
        "M5/constantify",
        "empty the roster entirely",
        _m5_empty_roster,
        threatens="an institution with no seats is not an institution",
    ),
    MutationOperator(
        "EQ/reorder-seats",
        "(equivalent) reverse the member order",
        _eq_reorder_members,
        should_be_killed=False,
    ),
    MutationOperator(
        "EQ/rewrite-prose",
        "(equivalent) rewrite descriptions and display names",
        _eq_rewrite_prose,
        should_be_killed=False,
    ),
    MutationOperator(
        "EQ/annotate",
        "(equivalent) add an unrecognized top-level annotation key",
        _eq_add_unknown_key,
        should_be_killed=False,
    ),
)


# --- runner ----------------------------------------------------------------------


def _default_probe(manifest: Manifest) -> None:
    parse_team_config(manifest, source="<mutant>")


def mutate(
    manifest: Manifest,
    *,
    source: str = "<memory>",
    operators: tuple[MutationOperator, ...] = OPERATORS,
    probe: Callable[[Manifest], Any] = _default_probe,
) -> MutationReport:
    """Apply every operator to a copy of ``manifest`` and report the outcome.

    ``probe`` is the check under test; it must raise to kill a mutant. The
    default is the real load path, so this measures the invariants a genuine
    run enforces rather than a parallel set written for the occasion.
    """
    report = MutationReport(source=source)
    for operator in operators:
        mutant = operator.apply(copy.deepcopy(manifest))
        if mutant is None:
            report.results.append(
                MutationResult(
                    operator=operator.name,
                    description=operator.description,
                    applicable=False,
                    should_be_killed=operator.should_be_killed,
                    killed=False,
                    threatens=operator.threatens,
                    detail="not applicable to this manifest",
                )
            )
            continue
        try:
            probe(mutant)
        except Exception as exc:  # noqa: BLE001 — any rejection kills the mutant
            killed, detail = True, f"{type(exc).__name__}: {exc}"
        else:
            killed, detail = False, "accepted"
        report.results.append(
            MutationResult(
                operator=operator.name,
                description=operator.description,
                applicable=True,
                should_be_killed=operator.should_be_killed,
                killed=killed,
                threatens=operator.threatens,
                detail=detail,
            )
        )
    return report


def mutate_file(path: Path | str, **kwargs: Any) -> MutationReport:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: manifest must be a mapping")
    return mutate(raw, source=str(path), **kwargs)
