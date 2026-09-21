# Architecture

Read `docs/CONCEPTS.md` first — this document assumes seats, authority,
counts-as, recovery, amendment and ledger, and shows where each lives in
code and what a run actually does.

## Dependency direction

```
your composition root  (harness/*.py, your own script, sciteam.cli)
        │  constructs and injects everything below
        ▼
Campaign ──► CampaignPlanner ──► MissionRunner ──► TeamOrchestrator
                 (adaptive_planner)   (mission)        (orchestrator)
                                          │                 │
                                  ContractValidator    AgentHeartbeat
                                     (mission)          (heartbeat)
                                                             │
                                                        RuntimePort
                                                        (runtime.py)
                                                             │
                                          your model / agent backend
```

Nothing below the composition root reaches upward and nothing constructs its
own IO. `RuntimePort` is the single seam between "institution" and "model" —
three async methods, no framework types. The institution layer never learns
which model answered for a seat, which is exactly why swapping backends
cannot change what counts.

## Module map

### Institution kernel — the mechanisms the paper is about

| Module | Responsibility |
|---|---|
| `team_loader.py` | Parse institution manifests; enforce load-time invariants (authority mutual exclusion, `judgment` stopping requires an assessor, some seat must be able to close, no negative round ceiling, roster policy). `build_registry` is the only sanctioned way in; `parse_team_config` applies the same checks to an in-memory manifest. |
| `coordination.py` | The declarative coordination vocabulary: topology (`chain` / `route` / `parallel` / `orchestrate` / `loop` / `hierarchy`), scheduling (`barrier` / `eager`), stopping (`judgment` / `verification`), aggregation, replicas, charter. |
| `worker_common.py` | `is_artifact_role` — the authority predicate. A seat that assesses the round can never also emit the exit artifact; a seat not listed in artifact-duty metadata can never submit. |
| `exit_artifact.py` | Writing and locating the artifact that closes a mission. |
| `audit_veto.py` | Schema-valid veto signals; `VetoUpstreamReviser` turns an *authorized* veto into a mechanical `REOPEN_UPSTREAM`. Unauthorized signals ignored, malformed ones fail closed, actioned vetoes not replayed. |
| `amendment.py` | Fail-closed layer derivation (G/C/L/P), preflight, promotion with before/after archival, `rollback_amendment` (refuses on drift). |
| `behavioral_shadow.py` | Simulate a candidate rule change against the authority predicate before promoting it; pass/fail/indeterminate, recorded — not the author's word. |
| `guard.py`, `claim_policy.py` | Runtime guard conditions and work-claim policy. |
| `registry_catalog.py` | Allowed paradigm / exit-contract ids are discovered from what ships on disk, never a hardcoded vocabulary in the engine. |

### Execution

| Module | Responsibility |
|---|---|
| `campaign.py` | Outer loop: planner proposes a mission, runner executes it, KB and trace record it, `BudgetClock` charges it. |
| `adaptive_planner.py` | The plan DAG and how it grows. `PlanReviser` / `CompositeReviser` make "how the plan reacts to outcomes" a pluggable policy rather than engine logic; `reopen_upstream` is the re-entry primitive recovery depends on. |
| `mission.py` | One mission end to end: preflight (capabilities, roster, skills), drive the orchestrator, validate the exit contract, produce a `MissionOutcome`. |
| `orchestrator.py` | One team run: rounds, work board, dependency graph, inboxes, round assessment. |
| `heartbeat.py` | Dispatches a seat's turn to the `RuntimePort`, claims work, merges results. |
| `depgraph.py`, `board.py`, `schedule.py`, `merge.py`, `aggregation.py` | Supporting mechanics for the above. |

### Adapters and IO

| Module | Responsibility |
|---|---|
| `runtime.py` | `RuntimePort` Protocol — the backend seam. Implement this to plug in any model or agent framework. |
| `resilience.py` | Composable `RuntimePort` resilience: per-attempt/total timeouts, exponential backoff with jitter, and stateful circuit breaker. Maps backend failures into first-class `AgentOutcome` states (G4.1). |
| `llm.py` | Stdlib-only OpenAI-compatible client (no `httpx`, no SDK). ~200 lines. |
| `llm_worker.py` | Reference `RuntimePort`: prompt assembly, tool loop, contract repair. |
| `wizard_tools.py`, `tool_catalog.py`, `tool_registry.py` | Tool declaration, permission, dispatch. Plugin-registered tools resolve here. |
| `sandbox.py`, `fs_tools.py`, `output_truncation.py` | Path guards and output discipline for tool execution. |
| `ports/` | `ComputePort` / `DatasetPort` / `LiteraturePort` / `VerifierPort` — Protocols only; implementations belong in your composition root or a plugin. |

### State and evidence

| Module | Responsibility |
|---|---|
| `event_log.py` | The institutional ledger: append-only, hash-chained `ledger.jsonl`, one chain per run. `verify_chain` is the tamper check (reports every break, not just the first); `replay` derives institutional state *from* the record; `cost_report` attributes per-call spend to seats. Reopening a file recovers the chain head, so a resumed run extends the same chain. |
| `checkpoint.py` | Resume after a crash by **replaying the record to rebuild state, never to redo acts**. Missions the ledger already records as decided are restored into the planner and the budget clock; the runner is never called for them, and a planner that re-proposes one is an error rather than a silent re-execution. |
| `provenance.py` | What produced a run, and what it could not pin. Records build/Python/dependency/plugin versions and `SCITEAM_*` overrides (credentials redacted), digests the pinned half only, and always reports an explicit `unpinnable` list — a hosted model endpoint can never be claimed reproducible. |
| `mutation.py` | Mutation testing for institutions: break a manifest on purpose and check the loader notices. Declares *equivalent* mutants that must survive, so the score measures strictness rather than brittleness. |

`kb.py` (campaign knowledge base), `evidence.py`, `trace.py`, `memory.py`,
`store.py`, `persist.py`, `experiment_pack.py` are the detailed views their
own mechanisms need; the event stream is the authoritative institutional
summary above them.

### Infrastructure

`plugin_api.py` (see `docs/PLUGIN-GUIDE.md`), `cli.py`, `_paths.py`
(bundled-data resolution), `canonical_demo.py` (the paper's run),
`config`-adjacent helpers (`limits.py`, `pricing.py`, `budget_gate.py`).

## Where the data lives

All runtime data ships **inside the package** under `sciteam/data/`:

```
sciteam/data/assets/teams/paradigms/*.yaml   21 coordination templates
sciteam/data/assets/prompts/                 constitution + role prompts
sciteam/data/assets/skills/                  skill definitions
sciteam/data/assets/roles/CATALOG.yaml       role registry
sciteam/data/assets/institutions/GRUNDNORM.md
sciteam/data/schemas/*.schema.json           exit contracts and signals
sciteam/data/contracts/
```

Resolve them through `sciteam._paths` (`DATA_ROOT`, `ASSETS_ROOT`,
`SCHEMAS_DIR`, `CONTRACTS_DIR`). **No module may use
`Path(__file__).parents[1] / "assets"`** — that works in a source checkout
and breaks in every real install; `tests/test_cli.py` fails the build if it
reappears.

Run output goes to `./runs/` (or `$SCITEAM_RUNS_ROOT`), never inside the
package. `SCITEAM_ASSETS_ROOT` overrides the asset root for callers that give
each organization its own evolvable copy; it fails fast on a half-empty
override rather than silently falling back.

## What a run does

Take `sciteam demo --mode full` (`canonical_demo.py`), which is the shortest
complete path through the ideas:

1. **Manifest** declares seats and the approval rule. Only `implementer`
   holds `emits_exit_artifact`; only `auditor` holds `may_veto_exit`.
2. A **bystander** with no seat submits a patch. `is_artifact_role` returns
   `False`. The patch exists on disk; institutionally nothing happened.
3. The **implementer** submits a buggy patch — authorized, so it counts as a
   submission.
4. The **auditor** runs the frozen tests in a subprocess. Pass/fail is owned
   by the verifier, not by any seat's narration. They fail.
5. The auditor writes an **authorized veto**. `VetoUpstreamReviser` emits
   `REOPEN_UPSTREAM` targeting `m_implement` — a plan edit, not a retry.
6. The implementer resubmits; tests pass; the **chair** closes against the
   declared approval rule.
7. `interpreter_hash_before == interpreter_hash_after`, and the same holds
   across `--config-swap`, where only the manifest differs.

Each of those steps is an entry in `ledger.jsonl`, chained and stamped with
the digest of the manifest in force, so none of it has to be taken on
trust:

```console
$ sciteam ledger show ./run
   3 x artifact_submitted   bystander      [may_emit_artifact]
   4 + artifact_accepted    implementer    [may_emit_artifact]
   5 + audit_reported       auditor        [may_assess_round]
   6 + veto_issued          auditor        [may_veto]
   7 + reopened             planner        [may_revise_plan]
   8 + artifact_accepted    implementer    [may_emit_artifact]
  10 + closed               chair          [may_close]
$ sciteam ledger verify ./run
ok    ./run/ledger.jsonl  11 entries, head e05565d7c96a073e…
```

The `x` on entry 3 is the whole argument in one character: the bystander's
patch physically exists, and the record says it never counted.

`--mode prompt_only | no_separation | no_recovery` remove one prop each and
produce three distinct failure modes. `tests/test_canonical_institution_demo.py`
pins the whole truth table; the CI `clean-install` job re-verifies it from a
freshly built wheel on a machine with no checkout and no pytest.

## Extension points

Five things are meant to change without editing `sciteam/`:

| You want to | Do this |
|---|---|
| use a different model or agent framework | implement `RuntimePort` |
| add a coordination pattern | write a manifest; `api.register_paradigm_dir` to ship it |
| add a tool | `api.register_tool` |
| implement a domain port | `api.register_port("compute" \| "dataset" \| "literature" \| "verifier", ...)` |
| observe or block an institutional event | `api.on("tool_call" \| "artifact_submitted" \| ...)` |

See `docs/PLUGIN-GUIDE.md`. The rule that makes this safe: **plugins add
constraints, never exemptions**, and they never override a built-in.

## Size and testing conventions

- Files stay under ~800 lines, classes ~300, functions ~60. Two modules
  (`llm_worker.py`, `mission.py`) currently exceed this and are being split.
- Tests assert **structural invariants**, never model wording. Unit tests use
  fakes (`ScriptedLlmClient`-style doubles, in-memory stores); no test makes
  a network call or needs credentials.
- `ruff check` / `ruff format --check` / `pytest` must all pass; CI runs them
  on 3.11–3.13 plus a clean-install smoke on Linux and macOS.
