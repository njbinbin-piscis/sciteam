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


class AmendmentSpec(_Strict):
    amendable: list[str]
    protected_rules: list[str] = Field(default_factory=list)
    proposers: str
    voters: str
    pass_: PassRule = Field(default_factory=PassRule, alias="pass")
    human_ratify: bool | list[str] = False    # true, or the paths whose changes need it
    voting_window_s: float = 3600

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
                    "progress")
FACT_KEYS = ("field", "distinct", "validated", "approved_by", "children_have")


VOCABULARY = """\
A rules file has six optional sections; every entry has a unique `name`.
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
- {progress: {validator: <v>, identical_failures: <k>}} — denied once the task's last k finished
  runs of validator v all failed with identical output: rework is not progressing; cancel the
  task or escalate to the owner (requirements)
- {validated: <validator>} — the validator passed on the task's latest submission (facts)
- {approved_by: <selector>} — whoever moved the task to done matches the selector (facts)
Selectors: role:<r>, kind:human|agent, team:<t>, group:<g>, a member id, task.assignee, task.reviewer,
task.creator, combined with | & ! and parentheses."""


class ProgressSpec(_Strict):
    validator: str
    identical_failures: int = Field(ge=2)


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
    progress: ProgressSpec | None = None

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
        if self.approved_by is not None:
            compile_selector(self.approved_by, {})
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
                for v in (c.validate_ or []) + ([c.validated] if c.validated else []) + \
                        ([c.progress.validator] if c.progress else []):
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
