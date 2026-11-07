"""Fold of the institution: rule-set history, pending Acts, validations, approvals, facts,
ledger and amendments (docs/40-institution.md §6–§9)."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sciteam.comm.model import Act
from sciteam.comm.view import COMM_EVENT_TYPES, CommView
from sciteam.institution.facts import Owed, derive
from sciteam.institution.model import RuleSet
from sciteam.kernel.events import Event

INST_STREAM = "institution"
INSTITUTION_EVENT_TYPES = {
    "rule_version_activated", "validation_requested", "validation_finished",
    "approval_requested", "approval_decided", "act_resolved", "fact_established",
    "fact_revoked", "ledger_entry", "amendment_proposed", "amendment_voted",
    "amendment_reviewed", "amendment_passed", "amendment_enacted", "amendment_closed",
    "environment_certified", "environment_revoked",
    "acceptance_set", "claim_recorded", "claim_replayed", "attestation_recorded",
    "adjudication_requested", "defect_recorded", "claim_replicated", "goal_verdict",
}
# Other events the fold needs to link domain events to the Acts that caused them.
LINK_EVENT_TYPES = {"act_submitted", "act_decided"}
FOLD_EVENT_TYPES = INSTITUTION_EVENT_TYPES | LINK_EVENT_TYPES | COMM_EVENT_TYPES


@dataclass
class Validation:
    id: str
    validator: str
    commit: str
    command: str
    act_id: str
    timeout_s: float
    seq: int = 0
    status: str = "requested"          # requested | passed | failed
    exit_code: int | None = None
    output_blob: str | None = None
    tail: str = ""
    done_seq: int = 0
    inputs: str | None = None          # digest of validator-visible files outside the commit


@dataclass
class ApprovalState:
    key: str                            # act id, or amendment id for ratification
    approval: str
    approver: str
    summary: str
    status: str = "requested"           # requested | approved | rejected
    by: str | None = None
    note: str = ""
    done_seq: int = 0
    requested_at: float = 0.0


@dataclass
class Amendment:
    id: str
    path: str
    content: str
    base_sha: str
    proposer: str
    rationale: str
    proposed_at: datetime
    votes: dict[str, str] = field(default_factory=dict)
    status: str = "open"                # trial | open | ratifying | enacted | closed
    reason: str = ""
    last_seq: int = 0                   # latest event that could change its outcome
    defects: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] | None = None
    review: dict[str, Any] | None = None   # re-adjudication of the recorded Acts
    diff: str = ""

    @property
    def loosens(self) -> bool:
        return bool(self.review and self.review.get("loosened"))


@dataclass
class Claim:
    id: str
    task_id: str
    name: str
    value: float
    commit: str
    actor: str
    spec_sha: str
    seq: int = 0
    status: str = "requested"          # requested | passed | failed
    code: str = ""                     # replay outcome, one of claims.REPLAY_CODES
    observed: Any = None
    tail: str = ""
    done_seq: int = 0
    # a reviewer's own reproduction {reproduce, file, pointer}; None = the spec's claim
    adhoc: dict[str, Any] | None = None
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class Attestation:
    id: str
    task_id: str
    claim_id: str
    name: str
    commit: str
    verdict: str                       # pass | reject | abstain
    basis: str
    counterexample: str | None
    by: str
    routed: str                        # "" | author | adjudication
    seq: int = 0


@dataclass
class Pending:
    act: Act
    waiting: list[str]
    decided_seq: int


class InstitutionView:
    """Fold in global order. It keeps its own CommView so that facts and ledger entries are
    derived from the state at their triggering event, whenever the fold runs (replay too)."""

    def __init__(self, constitution_path: str | None, comm_path: str):
        self.constitution_path, self.comm_path = constitution_path, comm_path
        self.comm = CommView()
        self.versions: list[tuple[int, RuleSet]] = []
        self.acts: dict[str, Act] = {}
        self.act_of_seq: dict[int, str] = {}          # act_decided / act_resolved seq -> act
        self.pending: dict[str, Pending] = {}
        self.validations: dict[str, Validation] = {}
        self.approvals: dict[str, ApprovalState] = {}
        self.last_submit: dict[str, str] = {}         # task -> act id of its latest submit
        self.facts: dict[tuple[str, str], int] = {}   # (fact, subject) -> establishing seq
        self.fact_ts: dict[tuple[str, str], float] = {}  # (fact, subject) -> established at
        self.ledger: list[dict[str, Any]] = []
        self.totals: dict[str, float] = defaultdict(float)
        self.owed: dict[tuple, Owed] = {}
        self.amendments: dict[str, Amendment] = {}
        self.certificates: dict[str, dict[str, Any]] = {}   # env -> environment_certified
        self.specs: dict[str, dict[str, Any]] = {}          # task id -> acceptance_set payload
        self.spec_by_sha: dict[str, Any] = {}               # spec sha -> AcceptanceSpec
        self.claims: dict[str, Claim] = {}
        self.attestations: dict[str, Attestation] = {}
        self.adjudications: dict[str, dict[str, Any]] = {}  # claim id -> latest request
        self.defects: dict[str, dict[str, Any]] = {}
        self.replications: dict[str, dict[str, Any]] = {}   # claim id -> latest replication
        self.done_by: dict[str, str] = {}                   # task -> act that made it done
        self.decided_seq: dict[str, int] = {}               # act -> seq of its final decision
        self.cursor = 0

    @property
    def rules(self) -> RuleSet | None:
        return self.versions[-1][1] if self.versions else None

    def ruleset_at(self, seq: int) -> RuleSet | None:
        cur = None
        for s, rs in self.versions:
            if s > seq:
                break
            cur = rs
        return cur

    def validations_for(self, act_id: str) -> dict[str, Validation]:
        return {v.validator: v for v in self.validations.values() if v.act_id == act_id}

    def spec(self, task_id: str):
        s = self.specs.get(task_id)
        return self.spec_by_sha.get(s["spec_sha"]) if s else None

    def claims_on(self, task_id: str, commit: str) -> dict[str, Claim]:
        """The latest claim per name for a task on a commit, under the task's spec in force."""
        s = self.specs.get(task_id)
        out: dict[str, Claim] = {}
        for c in self.claims.values():
            if c.task_id == task_id and c.commit == commit and s and \
                    c.spec_sha == s["spec_sha"] and c.adhoc is None:
                out[c.name] = c
        return out

    def review_commit(self, task_id: str) -> str | None:
        """The commit of the author's latest claim on the task under its spec in force."""
        s = self.specs.get(task_id)
        mine = [c for c in self.claims.values() if c.task_id == task_id and c.adhoc is None
                and s and c.spec_sha == s["spec_sha"]]
        return max(mine, key=lambda c: c.seq).commit if mine else None

    def latest_attestation(self, claim_id: str, by=None) -> Attestation | None:
        """The latest attestation of a claim, among members `by(member)` accepts."""
        hits = [a for a in self.attestations.values()
                if a.claim_id == claim_id and (by is None or by(a.by))]
        return max(hits, key=lambda a: a.seq) if hits else None

    def unattested(self, task_id: str, by) -> list[str]:
        """Claims of the review commit whose latest attestation by `by` is not a pass."""
        commit = self.review_commit(task_id)
        if commit is None:
            s = self.specs.get(task_id)
            return [f"no claims recorded under spec {s['spec_sha']}"] if s else []
        out = []
        for name, c in sorted(self.claims_on(task_id, commit).items()):
            a = self.latest_attestation(c.id, by)
            if a is None or a.verdict != "pass":
                out.append(f"{name} ({c.id}: {a.verdict + ' by @' + a.by if a else 'unattested'})")
        return out

    def returns(self, task_id: str) -> int:
        return sum(1 for a in self.attestations.values()
                   if a.task_id == task_id and a.routed == "author")

    def certificate(self, env: str) -> tuple[bool, str]:
        """(in force, explanation)."""
        cert, rs = self.certificates.get(env), self.rules
        if cert is None:
            return False, f"no '{env}' environment certificate has been issued"
        if rs is None or cert["fingerprint"] != rs.rules.env_fingerprint(env):
            return False, (f"the '{env}' certificate (issued by @{cert['by']}) no longer matches "
                           f"the probes in force; certify it again")
        return True, f"'{env}' certified by @{cert['by']} on commit {cert['commit'][:10]}"

    def has_fact(self, fact: str, subject: str) -> bool:
        """Recorded, or owed and not yet written (so derivations do not depend on timing)."""
        if (fact, subject) in self.facts:
            return not any(o.type == "fact_revoked" and o.payload["fact"] == fact and
                           o.payload["subject"] == subject for o in self.owed.values())
        return any(o.type == "fact_established" and o.payload["fact"] == fact and
                   o.payload["subject"] == subject for o in self.owed.values())

    # ---- fold ---------------------------------------------------------------------------
    def apply(self, ev: Event) -> None:
        self.cursor = ev.seq
        if ev.type in COMM_EVENT_TYPES:
            self.comm.apply(ev)
        handler = getattr(self, f"_on_{ev.type}", None)
        if handler is not None:
            handler(ev, ev.payload)
        rs = self.rules
        if rs is not None:
            for key, owed in derive(ev, rs, self):
                self.owed[key] = owed

    def _on_act_submitted(self, ev: Event, p: dict) -> None:
        act = Act.model_validate(p["act"])
        self.acts[act.act_id] = act

    def _on_act_decided(self, ev: Event, p: dict) -> None:
        self.act_of_seq[ev.seq] = p["act_id"]
        if p["decision"] == "pending":
            self.pending[p["act_id"]] = Pending(self.acts[p["act_id"]], p.get("waiting", []),
                                                ev.seq)
        else:
            self.decided_seq[p["act_id"]] = ev.seq

    def _on_act_resolved(self, ev: Event, p: dict) -> None:
        self.act_of_seq[ev.seq] = p["act_id"]
        self.pending.pop(p["act_id"], None)
        self.decided_seq[p["act_id"]] = ev.seq

    def _on_task_status_changed(self, ev: Event, p: dict) -> None:
        act_id = self.act_of_seq.get(ev.causation_id or -1)
        if p["transition"] == "submit" and act_id:
            self.last_submit[p["task_id"]] = act_id
        if p["to"] == "done" and act_id:
            self.done_by[p["task_id"]] = act_id

    def _on_rule_version_activated(self, ev: Event, p: dict) -> None:
        self.versions.append((ev.seq, RuleSet(self.constitution_path, self.comm_path,
                                              p["texts"])))

    def _on_validation_requested(self, ev: Event, p: dict) -> None:
        self.validations[p["validation_id"]] = Validation(
            id=p["validation_id"], validator=p["validator"], commit=p["commit"],
            command=p["command"], act_id=p["act_id"], timeout_s=p["timeout_s"], seq=ev.seq)

    def _on_validation_finished(self, ev: Event, p: dict) -> None:
        v = self.validations[p["validation_id"]]
        v.status = "passed" if p["ok"] else "failed"
        v.exit_code, v.output_blob, v.tail = p.get("exit_code"), p.get("output_blob"), \
            p.get("tail", "")
        v.inputs = p.get("inputs")
        v.done_seq = ev.seq

    def _on_approval_requested(self, ev: Event, p: dict) -> None:
        self.approvals[p["key"]] = ApprovalState(key=p["key"], approval=p["approval"],
                                                 approver=p["approver"], summary=p["summary"],
                                                 requested_at=ev.ts.timestamp())

    def _on_approval_decided(self, ev: Event, p: dict) -> None:
        a = self.approvals[p["key"]]
        a.status = "approved" if p["decision"] == "approve" else "rejected"
        a.by, a.note = p["by"], p.get("note", "")
        a.done_seq = ev.seq

    def _on_fact_established(self, ev: Event, p: dict) -> None:
        self.facts[(p["fact"], p["subject"])] = ev.seq
        self.fact_ts[(p["fact"], p["subject"])] = ev.ts.timestamp()
        self.owed.pop(("fact", p["rule"], ev.causation_id), None)

    def _on_fact_revoked(self, ev: Event, p: dict) -> None:
        self.facts.pop((p["fact"], p["subject"]), None)
        self.owed.pop(("revoke", p["rule"], ev.causation_id), None)

    def _on_ledger_entry(self, ev: Event, p: dict) -> None:
        self.ledger.append({**p, "seq": ev.seq, "ts": ev.ts})
        self.totals[p["member"]] += p["amount"]
        self.owed.pop(("ledger", p["rule"], ev.causation_id, p["member"]), None)

    def _on_amendment_proposed(self, ev: Event, p: dict) -> None:
        self.amendments[p["amendment_id"]] = Amendment(
            id=p["amendment_id"], path=p["path"], content=p["content"], base_sha=p["base_sha"],
            proposer=p["proposer"], rationale=p.get("rationale", ""), proposed_at=ev.ts,
            votes=dict(p.get("votes", {})), last_seq=ev.seq,
            defects=list(p.get("defects") or []), changes=p.get("changes"),
            review=p.get("review"), diff=p.get("diff", ""),
            status="trial" if p.get("trial") else "open")

    def _on_amendment_reviewed(self, ev: Event, p: dict) -> None:
        a = self.amendments[p["amendment_id"]]
        a.review, a.status, a.last_seq = p["review"], "open", ev.seq

    def _on_amendment_voted(self, ev: Event, p: dict) -> None:
        a = self.amendments[p["amendment_id"]]
        a.votes[p["voter"]] = p["choice"]
        a.last_seq = ev.seq

    def _on_amendment_passed(self, ev: Event, p: dict) -> None:
        self.amendments[p["amendment_id"]].status = "ratifying"

    def _on_amendment_enacted(self, ev: Event, p: dict) -> None:
        self.amendments[p["amendment_id"]].status = "enacted"

    def _on_amendment_closed(self, ev: Event, p: dict) -> None:
        a = self.amendments[p["amendment_id"]]
        a.status, a.reason = "closed", p["reason"]
        if p.get("review"):
            a.review = p["review"]

    def _on_environment_certified(self, ev: Event, p: dict) -> None:
        self.certificates[p["env"]] = {**p, "seq": ev.seq}

    def _on_environment_revoked(self, ev: Event, p: dict) -> None:
        self.certificates.pop(p["env"], None)

    def _on_acceptance_set(self, ev: Event, p: dict) -> None:
        from sciteam.institution.claims import parse_spec

        self.specs[p["task_id"]] = {**p, "seq": ev.seq}
        self.spec_by_sha[p["spec_sha"]] = parse_spec(p["spec"])

    def _on_claim_recorded(self, ev: Event, p: dict) -> None:
        self.claims[p["claim_id"]] = Claim(
            id=p["claim_id"], task_id=p["task_id"], name=p["name"], value=float(p["value"]),
            commit=p["commit"], actor=p["actor"], spec_sha=p["spec_sha"], seq=ev.seq,
            adhoc=p.get("adhoc"))

    def _on_claim_replayed(self, ev: Event, p: dict) -> None:
        c = self.claims[p["claim_id"]]
        c.status = "passed" if p["code"] == "ok" else "failed"
        c.code, c.observed, c.tail = p["code"], p.get("observed"), p.get("tail", "")
        c.sources = p.get("sources") or {}
        c.done_seq = ev.seq

    def _on_attestation_recorded(self, ev: Event, p: dict) -> None:
        self.attestations[p["attestation_id"]] = Attestation(
            id=p["attestation_id"], task_id=p["task_id"], claim_id=p["claim_id"],
            name=p["name"], commit=p["commit"], verdict=p["verdict"], basis=p.get("basis", ""),
            counterexample=p.get("counterexample"), by=p["by"], routed=p.get("routed", ""),
            seq=ev.seq)

    def _on_adjudication_requested(self, ev: Event, p: dict) -> None:
        self.adjudications[p["claim_id"]] = {**p, "seq": ev.seq}

    def _on_defect_recorded(self, ev: Event, p: dict) -> None:
        self.defects[p["defect_id"]] = {**p, "seq": ev.seq, "ts": ev.ts.isoformat()}

    def _on_claim_replicated(self, ev: Event, p: dict) -> None:
        self.replications[p["claim_id"]] = {**p, "seq": ev.seq, "ts": ev.ts.isoformat(),
                                            "at": ev.ts.timestamp()}
