# Concepts

The whole framework is one idea applied consistently:

> **A workflow or graph decides *what runs next*. An institution additionally
> decides *who is authorized to act*, *what counts*, *which rule version
> applies*, and *how the rules may legitimately change*.**

An agent doing something is a physical fact. Whether that something has any
effect *in the system* is a separate, institutional question. Most agent
frameworks collapse the two: if the model wrote the file, the file is
written; if the model said "tests pass", downstream steps proceed. SciTeam
keeps them apart and makes the second question answerable by code.

Six terms carry that idea. They recur in the manifest format, the API, the
ledger, and the plugin rules.

## Seat

A **seat** is a position in an institution, declared in a YAML manifest,
independent of whoever occupies it. `implementer`, `auditor`, `chair` are
seats; GPT-5 behind a `RuntimePort` is an occupant. The distinction is
load-bearing: swapping the model behind a seat changes who does the work and
changes nothing about what the work is allowed to do.

```yaml
members:
  - key: implementer
    role: implementer
    emits_exit_artifact: true      # this seat's authority
  - key: auditor
    role: auditor
    authority: [may_veto_exit]
  - key: chair
    role: chair
```

Seats are validated when the manifest is loaded, not when it is used —
`sciteam validate` runs exactly those checks. A seat that both emits the exit
artifact and assesses the round is rejected at load time, because that is
self-review wearing two hats.

## Authority

**Authority** is what a seat may do, checked mechanically. The three
authorities the shipped engine enforces:

| Authority | Meaning | Enforced in |
|---|---|---|
| `emits_exit_artifact` | may produce the output that closes the mission | `worker_common.is_artifact_role`, `exit_artifact.py` |
| `may_veto_exit` | may reject a submitted artifact and force re-entry | `audit_veto.py` |
| `may_assess_round` | may judge whether a round is finished | `round_assessor.py` |

Authority is not advice in a system prompt. `is_artifact_role` consults the
paradigm metadata derived from the manifest, so an agent that was never given
the seat cannot submit — regardless of what it was told, what it believes, or
how convincingly it narrates. In the canonical demo a bystander agent writes
a *correct* patch and it still does not count.

## Counts-as

**Counts-as** is the moment a produced artifact acquires system-level
validity. It is the hinge of the whole design: authority check plus contract
validation plus (where declared) a verifier-owned check. Only after all three
does the artifact become a fact the rest of the system may rely on.

The three ablations shipped with the demo each remove one prop and produce a
distinct, specific failure:

- `prompt_only` — no machine-checked validity, the chair accepts the
  implementer's own "done". Closes legally **with the bug still in the file**.
- `no_separation` — the implementer also audits. Same false positive, by a
  different route.
- `no_recovery` — no legal path back after a rejection. A correct veto
  becomes a dead end.

"Physical action ≠ institutional effect" is not a slogan; it is the
difference between these truth tables.

## Recovery

**Recovery** is what happens after something is rejected: failure, veto,
escalation, reopen. In most harnesses a failed check is an exception, a
retry, or a loop counter. Here it is an institutional act with an author.

An authorized veto (`may_veto_exit`) is written as a schema-valid signal;
`VetoUpstreamReviser` turns it into a `REOPEN_UPSTREAM` plan edit, which
re-enters the DAG at the vetoed node. Unauthorized veto signals are ignored
outright, malformed ones fail closed, and an already-actioned veto is not
replayed on the next planner tick. The repaired work re-enters the same
authority and counts-as checks it failed the first time — nothing is waved
through because it is the second attempt.

## Amendment

**Amendment** is changing the rules by the rules. Institutions that cannot be
changed get bypassed; institutions that anyone can change at any time are
decoration. SciTeam layers the rule base (see
`sciteam/data/assets/institutions/GRUNDNORM.md`):

| Layer | Who may change it | Example |
|---|---|---|
| **G** Grundnorm | frozen by the human operator; agents never | `GRUNDNORM.md` |
| **C** Constitution | human, between campaigns | `assets/prompts/constitution.md` |
| **L** Legislation | agents, within a campaign, via the amendment procedure | run-scoped institution files |
| **P** Practice | evolvable skills, with audit hooks | `assets/skills/*` |

`amendment.py` derives the layer from the target path **fail-closed**: a path
that matches no whitelist entry, or resolves to G, is refused. Before
promotion, `behavioral_shadow.py` instantiates the authority predicate
against both the incumbent and the candidate rule tree and flags any
authority property the candidate breaks. After promotion,
`rollback_amendment` can mechanically reverse it from an archived
before/after pair — refusing to run if the current content has since drifted,
rather than clobbering an unrelated later edit.

## Ledger

The **ledger** is the append-only record of what happened institutionally:
proposals, approvals, vetoes, rule versions, evidence, reopens, final
effects. It is what makes the other five terms auditable after the fact
rather than claims about a process nobody can inspect.

`sciteam.event_log` implements it as a hash-chained JSONL stream
(`ledger.jsonl`, one chain per run, schema
`sciteam/data/schemas/institution_event.schema.json`). Every entry states
who acted (`seat`), under what right (`authority`), whether the act counted
(`effect`), and against which frozen rule text it was judged
(`rule_version`) — and commits to its predecessor's hash:

```bash
sciteam demo --mode full --out ./run        # writes ./run/ledger.jsonl
sciteam ledger verify ./run                 # exit 1 if anything was edited
sciteam ledger show ./run                   # '+' counted, 'x' refused
sciteam ledger replay ./run                 # the state the chain implies
sciteam ledger cost ./run                   # per-call spend, by seat
```

Three properties do the work. It is **append-only** — a mistaken entry is
corrected by appending a correcting entry, exactly as an institution
corrects a decision by making another decision. It is **tamper-evident** —
editing, reordering or dropping an entry invalidates every entry after it,
and `verify_chain` reports where. And it is **replayable** — `replay()`
derives which artifacts hold effect, which acts were refused and what was
reopened *from* the record, so state cannot silently disagree with the
evidence behind it.

`EventLog` recovers the chain head when it reopens a file, so a resumed run
extends the same chain instead of starting a second one. That is what makes
crash recovery institutional rather than merely operational: a mission whose
outcome was accepted produced an *effect*, so `Campaign.run(resume=True)`
restores it from the ledger and never runs it again — re-running it would
either duplicate the effect or silently replace it, and either way the record
would stop describing what happened. `sciteam ledger resume-state <run>`
answers "what will actually re-run if I restart this?" before you do.

Plugins may append
their own `x_`-prefixed kinds; those are chained and verified like any other
entry but carry no interpreter semantics, so a plugin can add observations
without minting new institutional effects.

The older per-mechanism records (`amendments.jsonl`, planner feedback
events, traces) still exist as the detailed views their own mechanisms need;
the event stream is the authoritative institutional summary above them.

## How the pieces sit in the architecture

Four layers, each independently testable:

| Layer | Responsibility | Where |
|---|---|---|
| **Specification** | seats, authority, protocol, exit contract, re-entry, amendment format | manifests under `sciteam/data/assets/`, `coordination.py` |
| **Interpreter** | schedules seats, enforces counts-as, rejects unauthorized acts, drives amendments | `orchestrator.py`, `amendment.py`, `audit_veto.py` |
| **Agent adapter** | binds a model/agent backend to a seat; holds no institutional effect itself | `runtime.py` (`RuntimePort`), `llm_worker.py` |
| **Ledger** | records the above, tamper-evidently | `event_log.py`; `ledger.jsonl` in run directories under `./runs/` |

The separation between Interpreter and Adapter is what makes the
config-swap demonstration possible: switch single-approver to a two-auditor
committee and the institution's *effect* changes while the interpreter stays
byte-identical (`sciteam demo --mode full --config-swap` reports
`interpreter_hash_unchanged: true`). Authority and validity live in the
Interpreter, driven by data in the Specification — never in the Adapter.

## Further reading

- `docs/ARCHITECTURE.md` — module map and the paths a run actually takes.
- `docs/PLUGIN-GUIDE.md` — extending the system without editing `sciteam/`.
- `sciteam demo --mode full` — all six terms in one 0.9-second run.
