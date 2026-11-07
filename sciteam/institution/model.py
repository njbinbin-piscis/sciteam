"""Constitution and rules schemas, the closed condition vocabulary, and the rule set with its
version (docs/40-institution.md §2–§5)."""

from hashlib import sha256
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sciteam.comm.policy import CommPolicy, compile_selector


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PassRule(_Strict):
    yes_at_least: int | Literal["all"] = "all"
    no_at_most: int = 0

    @model_validator(mode="after")
    def _valid_quorum(self):
        if isinstance(self.yes_at_least, bool) or (
            isinstance(self.yes_at_least, int) and self.yes_at_least < 1
        ):
            raise ValueError("yes_at_least must be 'all' or an integer >= 1")
        if self.no_at_most < 0:
            raise ValueError("no_at_most must be >= 0")
        return self


class Admission(_Strict):
    """What re-adjudicating the project's recorded Acts under a proposed rule set must show
    before the amendment opens for voting (docs/09 §5.2)."""
    fixed_at_least: int = Field(default=1, ge=0)
    regressed_at_most: int = Field(default=1, ge=0)
    regressed_fraction: float = Field(default=0.02, ge=0, le=1)

    def regressed_limit(self, allowed: int) -> int:
        return max(self.regressed_at_most, int(self.regressed_fraction * allowed))


class AmendmentSpec(_Strict):
    amendable: list[str]
    # "<rule>": the rule stays present and unchanged; "<rule>:<key>": the rule stays present
    # and keeps every condition with that key
    protected_rules: list[str] = Field(default_factory=list)
    proposers: str
    voters: str
    pass_: PassRule = Field(default_factory=PassRule, alias="pass")
    human_ratify: bool | list[str] = False    # true, or the paths whose changes need it
    voting_window_s: float = 3600
    admission: Admission | None = None         # for amendments of the rules file

    @model_validator(mode="after")
    def _selectors(self):
        compile_selector(self.proposers, {})
        compile_selector(self.voters, {})
        return self

    def ratify(self, path: str) -> bool:
        return self.human_ratify is True or (isinstance(self.human_ratify, list)
                                             and path in self.human_ratify)


class Constitution(_Strict):
    name: str
    principles: str = ""
    rules: str | None = None                  # path of the rules file
    amendment: AmendmentSpec | None = None


# ---- rules file ------------------------------------------------------------------------

ROLE_REFS = ("actor", "task.assignee", "task.reviewer", "task.creator")
TASK_FIELDS = ("acceptance", "reviewer", "estimate_h", "labels")
REQUIREMENT_KEYS = ("field", "evidence", "validate", "distinct", "children_have", "certificate",
                    "spec_typed", "claims_cover", "claims_replayed", "claims_attested",
                    "independent_of", "holds_at_most")
FACT_KEYS = ("field", "distinct", "validated", "approved_by", "children_have", "claims_attested")


VOCABULARY = """\
A rules file has seven optional sections; every entry has a unique `name`.
- validators: {<name>: {command, timeout_s}} — a shell command run in a clean copy of the
  evidence commit, without network; exit code 0 = pass. "{task.acceptance}" is replaced by the
  task's acceptance command.
- requirements: [{name, act, transition?, where?, require: [conditions]}] — conditions an Act
  must meet; unmet = denied, naming the rule. act: task_create | task_update | task_transition |
  post_message | ...; transition: e.g. "submit" or "approve|reject".
- approvals: [{name, act, transition?, where?, approver: <selector>}] — the Act waits until a
  member matching the selector approves it.
- facts: [{name, when: {event, transition?, to?}, require: [conditions], revoke_when?}] — what
  counts as an institutional fact (e.g. a task accepted), derived from task_status_changed events.
- ledger: [{name, when: {fact | validation: passed|failed | transition}, to: task.assignee |
  task.reviewer | task.creator | act.actor, amount}] — ledger entries.
- environments: {<name>: {probes: [<validator>, ...]}} — an environment certificate: the Act
  certify_environment(env, commit) runs those validators (probes) and, if all pass, the
  certificate is in force until revoked or until a probe's definition changes.
- rework_rounds: <n> (default 1) — how many times an attest reject with a counterexample returns
  a task to its author; past that, and for every abstain, the claim goes to adjudication
  (members with role adjudicator, else the humans).
where: {task_label: <label>} and/or {task_is_root: true|false}.
Conditions (exactly one key each):
- {field: acceptance|reviewer|estimate_h|labels} — the task has that field set (requirements, facts)
- {evidence: commit} — the Act names a commit of /workspace (requirements)
- {validate: [<validator>, ...]} — those validators pass on the Act's commit (so it implies
  evidence: an Act without commit=... is denied); the Act waits for them
  (requirements)
- {distinct: [a, b]} — two of actor, task.assignee, task.reviewer, task.creator are different
  members (requirements, facts)
- {children_have: <fact>} — every non-cancelled subtask has that fact (requirements, facts)
- {certificate: <environment>} — that environment certificate is in force (requirements)
- {spec_typed: true} — the task has a typed acceptance spec in force, set by an allowed
  propose_acceptance (requirements)
- {claims_cover: true} — every claim the spec names is recorded (record_claim) for the task on
  the Act's commit (requirements; implies evidence)
- {claims_replayed: true} — every claim the spec names, recorded on the Act's commit, replays:
  the spec's reproduce command reruns on a clean export and the value read by JSON Pointer
  matches; the Act waits for running replays (requirements; implies claims_cover)
- {claims_attested: <selector>} — every claim of the task's latest claim commit has, as its latest
  attestation by a member matching the selector, verdict pass (requirements, facts)
- {independent_of: author} — the actor is not the task's assignee and cannot read the
  assignee's private directory (no reads_private naming it) (requirements, e.g. on attest)
- {holds_at_most: <n>} — the actor holds at most n other unfinished tasks (assigned,
  in_progress or blocked), e.g. on claim so that a member takes one task at a time
  (requirements)
- {validated: <validator>} — the validator passed on the task's latest submission (facts)
- {approved_by: <selector>} — whoever moved the task to done matches the selector (facts)
Selectors: role:<r>, kind:human|agent, team:<t>, group:<g>, a member id, task.assignee, task.reviewer,
task.creator, combined with | & ! and parentheses."""


class Condition(_Strict):
    """Exactly one key set; the vocabulary is closed (40 §5)."""
    field: str | None = None
    evidence: Literal["commit"] | None = None
    validate_: list[str] | None = Field(default=None, alias="validate")
    distinct: list[str] | None = None
    validated: str | None = None
    approved_by: str | None = None
    children_have: str | None = None
    certificate: str | None = None
    spec_typed: Literal[True] | None = None
    claims_cover: Literal[True] | None = None
    claims_replayed: Literal[True] | None = None
    claims_attested: str | None = None
    independent_of: Literal["author"] | None = None
    holds_at_most: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one(self):
        set_ = [k for k, v in self.model_dump(by_alias=True).items() if v is not None]
        if len(set_) != 1:
            raise ValueError(f"a condition has exactly one key, got {set_ or 'none'}")
        if self.field is not None and self.field not in TASK_FIELDS:
            raise ValueError(f"field must be one of {TASK_FIELDS}")
        if self.distinct is not None and (len(self.distinct) != 2 or
                                          any(r not in ROLE_REFS for r in self.distinct)):
            raise ValueError(f"distinct takes two of {ROLE_REFS}")
        for sel in (self.approved_by, self.claims_attested):
            if sel is not None:
                compile_selector(sel, {})
        return self

    @property
    def key(self) -> str:
        return next(k for k, v in self.model_dump(by_alias=True).items() if v is not None)


class Where(_Strict):
    task_label: str | None = None
    task_is_root: bool | None = None


class ActMatch(_Strict):
    act: str
    transition: str | None = None
    where: Where | None = None


class Requirement(ActMatch):
    name: str
    require: list[Condition]


class Approval(ActMatch):
    name: str
    approver: str

    @model_validator(mode="after")
    def _sel(self):
        compile_selector(self.approver, {})
        return self


class ValidatorSpec(_Strict):
    command: str
    timeout_s: float = 900


class EventMatch(_Strict):
    event: str
    transition: str | None = None
    to: str | None = None


class FactRule(_Strict):
    name: str
    subject: Literal["task"] = "task"
    when: EventMatch
    require: list[Condition] = Field(default_factory=list)
    revoke_when: EventMatch | None = None


class LedgerWhen(_Strict):
    fact: str | None = None
    validation: Literal["passed", "failed"] | None = None
    transition: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if sum(v is not None for v in (self.fact, self.validation, self.transition)) != 1:
            raise ValueError("ledger `when` takes exactly one of fact, validation, transition")
        return self


class LedgerRule(_Strict):
    name: str
    when: LedgerWhen
    to: Literal["task.assignee", "task.reviewer", "task.creator", "act.actor"]
    amount: float


class EnvironmentSpec(_Strict):
    probes: list[str] = Field(min_length=1)


class RulesFile(_Strict):
    validators: dict[str, ValidatorSpec] = Field(default_factory=dict)
    environments: dict[str, EnvironmentSpec] = Field(default_factory=dict)
    requirements: list[Requirement] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    facts: list[FactRule] = Field(default_factory=list)
    ledger: list[LedgerRule] = Field(default_factory=list)
    rework_rounds: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _refs(self):
        names = [r.name for r in (*self.requirements, *self.approvals, *self.facts,
                                  *self.ledger)]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate rule names {sorted(dup)}")
        facts = {f.name for f in self.facts}
        for r in (*self.requirements, *self.facts):
            allowed = FACT_KEYS if isinstance(r, FactRule) else REQUIREMENT_KEYS
            for c in r.require:
                if c.key not in allowed:
                    raise ValueError(f"rule {r.name}: '{c.key}' cannot be used here; "
                                     f"allowed {allowed}")
                for v in (c.validate_ or []) + ([c.validated] if c.validated else []):
                    if v not in self.validators:
                        raise ValueError(f"rule {r.name}: unknown validator '{v}'")
                if c.children_have and c.children_have not in facts:
                    raise ValueError(f"rule {r.name}: unknown fact '{c.children_have}'")
                if c.certificate and c.certificate not in self.environments:
                    raise ValueError(f"rule {r.name}: unknown environment '{c.certificate}'")
        for env, spec in self.environments.items():
            for v in spec.probes:
                if v not in self.validators:
                    raise ValueError(f"environment {env}: unknown validator '{v}'")
        for lr in self.ledger:
            if lr.when.fact and lr.when.fact not in facts:
                raise ValueError(f"ledger {lr.name}: unknown fact '{lr.when.fact}'")
        return self

    def named(self) -> dict[str, dict[str, Any]]:
        """Every rule by name, as data, with the validators it runs (so a protected rule cannot
        be hollowed out by changing a validator's command)."""
        out = {}
        for r in (*self.requirements, *self.approvals, *self.facts, *self.ledger):
            data = r.model_dump(mode="json", by_alias=True)
            used = [v for c in getattr(r, "require", [])
                    for v in (c.validate_ or []) + ([c.validated] if c.validated else [])
                    + (self.environments[c.certificate].probes if c.certificate else [])]
            if used:
                data["validators"] = {v: self.validators[v].model_dump(mode="json")
                                      for v in used}
            out[r.name] = data
        return out

    def env_fingerprint(self, env: str) -> str | None:
        """What a certificate attests: its probes and their commands. A certificate issued
        under another fingerprint is no longer in force."""
        spec = self.environments.get(env)
        if spec is None:
            return None
        return text_sha("\n".join(f"{v} {self.validators[v].command}" for v in spec.probes))[:12]


def text_sha(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def protected_changes(protected: list[str], old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """The protected entries a change from `old` to `new` (RulesFile.named()) would break."""
    out = []
    for entry in protected:
        rule, _, key = entry.partition(":")
        if rule not in old:
            continue
        if not key:
            if new.get(rule) != old[rule]:
                out.append(entry)
            continue
        kept = (new.get(rule) or {}).get("require", [])
        if rule not in new or any(c for c in old[rule].get("require", [])
                                  if c.get(key) is not None and c not in kept):
            out.append(entry)
    return out


# Rule-set files of higher layers, by path prefix (e.g. "org/" -> the organization manifest).
FILE_KINDS: dict[str, Any] = {}


class RuleSet:
    """The texts of the constitution, rules file and communication policy in force, parsed.
    Built only from texts, so any past version can be rebuilt from the event store."""

    def __init__(self, constitution_path: str | None, comm_path: str, texts: dict[str, str]):
        self.constitution_path, self.comm_path = constitution_path, comm_path
        self.texts = dict(texts)
        self.constitution = (Constitution.model_validate(yaml.safe_load(texts[constitution_path])
                                                         or {})
                             if constitution_path else None)
        self.rules_path = self.constitution.rules if self.constitution else None
        self.rules = parse_file(self.rules_path, texts[self.rules_path], self.rules_path) \
            if self.rules_path else RulesFile()
        self.comm = parse_file(comm_path, texts[comm_path], self.rules_path)
        self.extra = {p: parse_file(p, t, self.rules_path) for p, t in texts.items()
                      if p not in (constitution_path, self.rules_path, comm_path)}
        files = sorted((p, text_sha(t)) for p, t in self.texts.items())
        self.files = dict(files)
        self.version = text_sha("\n".join(f"{p} {s}" for p, s in files))[:12]

    def with_file(self, path: str, text: str) -> "RuleSet":
        return RuleSet(self.constitution_path, self.comm_path, {**self.texts, path: text})

    def apply_amendment(self, path: str, content: str) -> "RuleSet":
        """Validate and apply an amendment to `path`, enforcing `amendable` and `protected_rules`."""
        spec = self.constitution.amendment if self.constitution else None
        if spec is None:
            raise ValueError("the constitution does not allow amendments")
        if path not in spec.amendable:
            raise ValueError(f"'{path}' is not amendable; amendable: {spec.amendable}")
        if path not in self.texts:
            raise ValueError(f"'{path}' is not part of the rule set in force")
        if not content.strip():
            raise ValueError("give the complete new content of the file")
        if text_sha(content) == self.files[path]:
            raise ValueError(f"the content is identical to {path} in force")
        new = self.with_file(path, content)
        changed = protected_changes(spec.protected_rules, self.rules.named(), new.rules.named())
        if changed:
            raise ValueError(f"protected rules must stay present and unchanged: {changed}")
        # Anti-self-lockout guard (RT-4): if an org/ manifest is amended, seats must remain
        # non-empty and every role:<seat> referenced in amendment proposers/voters must exist.
        for p, obj in new.extra.items():
            if p.startswith("org/") and hasattr(obj, "seats"):
                seat_names = {s.name for s in obj.seats}
                if not seat_names:
                    raise ValueError(f"amendment to '{p}' would leave 0 seats (self-lockout)")
                import re

                req_roles = set(
                    re.findall(r"role:([a-zA-Z0-9_-]+)", f"{spec.proposers} {spec.voters}")
                )
                missing_roles = sorted(req_roles - seat_names)
                if missing_roles:
                    raise ValueError(
                        f"amendment to '{p}' removes seat(s) {missing_roles} required by "
                        f"amendment proposers/voters (self-lockout)"
                    )
                total_seats = sum(s.count for s in obj.seats)
                if isinstance(spec.pass_.yes_at_least, int) and spec.pass_.yes_at_least > total_seats:
                    raise ValueError(
                        f"amendment to '{p}' reduces total seats ({total_seats}) below "
                        f"yes_at_least ({spec.pass_.yes_at_least}) (self-lockout)"
                    )
        return new


def parse_file(path: str, text: str, rules_path: str | None):
    """Load a rule-set file with the schema its role requires; raises on invalid content."""
    data = yaml.safe_load(text) or {}
    for prefix, schema in FILE_KINDS.items():
        if path.startswith(prefix):
            return schema.model_validate(data)
    if path == rules_path:
        return RulesFile.model_validate(data)
    if path.startswith("institution/"):
        return Constitution.model_validate(data)
    if path.startswith("comm/"):
        return CommPolicy.model_validate(data)
    raise ValueError(f"no schema registered for rule-set file '{path}' (import the layer "
                     f"that defines it)")
