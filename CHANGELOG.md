# Changelog

All notable changes to SciTeam are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) from 0.2.0 onward (pre-1.0: a
breaking change bumps the **minor** version). `PLUGIN_API_VERSION`
(`sciteam.plugin_api`) is versioned independently — see
`docs/COMPATIBILITY.md`.

## [Unreleased]

### Added
- **E7 — Single-machine product hardening (`0.3.0.dev0`)**.
  - **Cold-start Team/Mission recovery** (`sciteam/recovery.py`).
    `TeamOrchestrator.cold_resume(team_run_id)` reconstructs `TeamRun`/
    `WorkBoard`/`DepGraph`/undelivered inboxes from `sciteam.persist`'s
    on-disk snapshot (`team_run.json`/`board.json`/`deps.jsonl`) into a
    fresh orchestrator instance after a process crash/restart. A work item
    the snapshot already records as `DONE`/`FAILED` can never be re-claimed
    (`WorkBoard._transit` only allows `READY -> CLAIMED`), so a cold restart
    cannot re-decide a mission whose result already counted.
    `TeamRun.to_dict()`/`from_dict()` and `AgentSlot.to_dict()`/`from_dict()`
    are now full-fidelity, lossless round-trips.
  - **Unattended-by-default sandbox profile** (`sciteam/sandbox.py`).
    `compute.sandbox`/`model.certify` now resolve a `sandbox_profile`
    (`unattended` default via `SCITEAM_SANDBOX_PROFILE` env or explicit
    arg) through `build_sandbox()`. The `unattended` profile requires the
    `sciteam-sandbox-docker` plugin and fails closed
    (`SandboxUnavailableError` / `reason_code=sandbox_unavailable`) rather
    than silently degrading to the uncontained `LocalPythonSandbox` when
    Docker is missing. `development` remains available as an explicit,
    logged opt-in for interactive local debugging.
  - **Python `Session` SDK** (`sciteam/session_sdk.py`). Embeddable,
    institution-independent `create/run/resume/fork/events/close`, built
    directly on the E4/E5 primitives (`FileSessionStore`,
    `MechanicalCompactionPort`, `SimpleTokenMeter`) — not a second loop
    with its own rules. Compaction fires automatically under context
    pressure mid-`run()`; pins never enter the summarized region.
  - **Structured logging + end-of-run summary**
    (`sciteam/logging_setup.py`, `sciteam/run_summary.py`).
    `configure_structured_logging()` attaches an idempotent JSON-lines
    handler to the `sciteam` logger tree. `build_run_summary()`/
    `write_run_summary()` compose institutional decisions
    (`sciteam.checkpoint`), attributed spend (`sciteam.event_log.cost_report`)
    and `ResilientRuntimePort.metrics()` into one JSON artifact; a reserved
    `ObserverPort` (default: `NullObserver`, a documented no-op) is the
    seam a future OTel/Prometheus exporter would attach to — none ships
    in v1.
  - **Provider 429/5xx retry widened** (`sciteam/resilience.py`).
    `ResilientRuntimePort` now also retries when
    `sciteam.overflow.classify_error(exc)` returns `rate_limited`,
    `server_error`, or `timeout` — previously only four built-in exception
    *types* were retryable, which missed vendor-specific rate-limit
    exception classes that raise with a 429 status/message but not one of
    those types.
  - **Plugin conflict all-or-nothing fix** (`sciteam/plugin_api.py`).
    `PluginRegistry._merge_from` previously committed a plugin's staged
    registrations into the final registry one at a time, so a tool
    registered before a later colliding one stayed committed even though
    the plugin's manifest was ultimately rejected. `_check_merge_conflicts`
    now validates the *entire* staged batch first; a plugin with any single
    conflicting registration now leaks none of its other registrations.
  - **Product contract test suite** (`tests/test_product_contracts.py`,
    `tests/test_recovery.py`, `tests/test_sandbox_profile.py`,
    `tests/test_session_sdk.py`, `tests/test_run_summary.py`,
    `tests/test_logging_setup.py`). Covers cold-start recovery, unattended
    sandbox refusal, provider 429 retry (including a check that unrelated
    fatal errors are *not* over-retried), session-run cancellation leaving
    a resumable log, duplicate mission-decision resubmission not doubling
    an institutional effect, ledger tamper detection, and a 500-round soak
    with periodic simulated crash + cold restart (no corruption, no replay,
    no unbounded snapshot growth).
  - **`MissionRunner`/`TeamOrchestrator` resume-or-create** (E7 follow-up).
    `TeamOrchestrator.create_or_resume_run(spec)` and
    `MissionRunner.run(spec, team_run_id=...)` are the missing wiring that
    turns `cold_resume` from a standalone recovery primitive into something
    a caller can actually use: given a caller-supplied stable `team_run_id`,
    a re-invocation (e.g. a crashed process replaying its still-undecided
    mission) resumes the exact prior `WorkBoard`/`DepGraph` progress instead
    of starting a second, indistinguishable run. Omitting `team_run_id` (the
    default) is byte-for-byte the previous behavior. `create_run` itself now
    rejects a `team_run_id` that collides with an existing/resumable run
    rather than silently resetting it.
- **E4 — Session kernel (`sciteam/session.py`, `docs/SESSION-KERNEL.md`)**.
  Append-only, hash-chained `SessionEvent` log distinct from the institution
  ledger (`event_log.py`): closed `kind` vocabulary, `FileSessionStore`
  (torn-write tolerant on resume, verified by a real `SIGKILL` mid-session
  test), `fork` for exploration history (never institutional), content-
  addressed archive for pruned tool results, and a pure/deterministic
  `project()` that rebuilds model-visible context with mechanical tool
  call/result pairing (pending vs. crash-interrupted). Wired additively into
  `tool_loop.py`/`llm_worker.py` via an optional `session_store` — `None`
  (the default) is a byte-identical no-op for every existing caller.
- **E5 — Compaction, token accounting, overflow recovery**
  (`sciteam/token_meter.py`, `sciteam/compaction_port.py`,
  `sciteam/overflow.py`). `TokenReading` distinguishes provider-reported
  usage from a heuristic estimate (`estimated` is never silently dropped);
  `MechanicalCompactionPort` folds session history behind an injected
  `Summarizer` port with a hard non-shrinking rejection guard and a bounded
  retry wrapper (never loops forever on a broken summarizer); pins
  (constitution/authority/rule version/open vetoes/cited evidence) have no
  code path into the summarized text. `read_archived_result` — a capability-
  gated, always-on recovery tool — lets any seat reopen a compacted tool
  result by its content-addressed pointer. `overflow.classify_error` +
  `OverflowRecoveryPolicy` implement the "one compaction retry then give up
  as a first-class failure" state machine.
- **E6 — Plugin hot path + single composition root** (`sciteam/composition.py`).
  The two blockable institutional hooks (`tool_call`, `artifact_submitted`)
  now actually fire on the real mission-worker inner loop and the real
  artifact-commit path — previously exercised only by tests and example
  scripts. `wizard_tools.tool_definitions_for`/`tool_permission`/
  `tool_deterministic`/`run_research_tool` gained an explicit `plugins=`
  fallback (built-ins first, never shadowed) so an installed plugin's tool
  is visible to the mission worker, not only to the intake wizard.
  `CompositionRoot` centralizes plugin loading, `ResilientRuntimePort`
  wrapping, and session-store/plugin-registry injection; `canonical_demo.py
  --runtime` now resolves through it, so a live-provider canonical run gets
  the same timeout/retry/circuit-breaker protection every other path gets.
- **Production Reference Deployments (`examples/`)**.
  - `examples/github_pr_review`: Production reference deployment of GitHub PR review
    and merge governance, featuring mechanical separation of powers, adversarial
    security red-teaming, authorized vetoes on secret leakage, automated task reopening,
    committee closure rules, and verifiable hash-chained ledger generation.
  - `examples/research_lab_tournament`: Autonomous research lab tournament featuring
    hypothesis generation, skeptical falsifiability critique, blind pairwise ELO ranking,
    literature proximity novelty auditing, and consensus meta-review synthesis.
- **`docs/PARADIGMS-GUIDE.md`: Paradigm Gallery & Decision Matrix**.
  Comprehensive guide covering all 21 bundled paradigms, categorized by coordination
  topology (Route, Loop, Chain, Council, Fan-Out, Adversarial, Workbench) with an
  authoritative "When to USE / When NOT to USE" decision matrix.
- **Official Provider Plugins Matrix (`plugins/`)**.
  - `sciteam-runtime-anthropic`: Official Anthropic Claude `RuntimePort` provider
    plugin supporting `claude-3-5-sonnet-20241022`, `claude-3-7-sonnet-latest`,
    native tool-use translation, and first-class disposition mappings (401 fatal,
    429/529 recoverable).
  - `sciteam-runtime-deepseek`: Official DeepSeek `RuntimePort` provider plugin
    supporting `deepseek-chat` and `deepseek-reasoner` (R1). Automatically isolates
    `reasoning_content` (chain-of-thought) into `RunSpec.metadata` for institutional
    auditing without polluting exit artifacts, and maps 402 balance exhaustion to
    `AgentOutcome.FATAL`.
  - `sciteam-tool-mcp`: Official Model Context Protocol (MCP) tool bridge plugin,
    mapping dynamic MCP server tools into `ToolSpec` instances with capability tags
    and permissions, gated by SciTeam's `tool_call` institutional hooks.
  - `sciteam-sandbox-docker`: Official Docker container sandbox plugin implementing
    `SandboxPort` (`run` and `certify`), executing programs under hermetic Linux
    container isolation driven by manifest `guard` declarations.
  - Multi-backend dual-run institutional isomorphism test suite
    (`tests/test_multi_backend_isomorphism.py`) proving that identical institutional
    manifests produce structurally isomorphic validity event streams across
    distinct LLM backends (Anthropic Claude and DeepSeek R1).
  - `sciteam demo --runtime <provider>` CLI flag allowing canonical runs to be
    driven directly by registered `RuntimePort` plugins, recording physical telemetry
    and token usage into the tamper-evident ledger.
  - Integration test suite (`tests/test_official_plugins.py`) and dedicated CI
    matrix job.
- **`sciteam init`: project scaffolding from 21 bundled paradigms.**
  Generates a runnable, testable institutional project from any coordination
  template (`sciteam init <template> [-t DIR] [-n NAME]`). Copies and adapts
  the manifest (`institution.yaml`), generates role prompts for every seat,
  emits a frozen test suite (`test_institution.py`) checking load invariants
  and mutation survivability (`score=1.0`), and produces an informative
  `README.md`. `sciteam init --list` lists all available templates.
- **`sciteam ledger view`: zero-dependency interactive HTML trajectory.**
  Exports a self-contained, offline-ready HTML view of any institutional ledger
  (`sciteam ledger view <run | ledger.jsonl> [-o path] [--open]`). Contains
  interactive filtering by seat, event kind, and effect, formatted JSON payload
  inspection, and a one-click "Highlight Veto & Recovery Chain" spotlight.
  Loads zero external CDNs or scripts, fulfilling air-gapped security policies.
- **`sciteam.resilience`: timeout, retry, circuit breaker for `RuntimePort`.**
  A composable resilience layer wrapping any `RuntimePort` backend. Enforces
  per-attempt and total invocation timeouts via `asyncio.wait_for`, preventing
  hung external providers from stalling execution. Implements exponential
  backoff with decorrelated jitter to avoid thundering herds against rate
  limits. A stateful `CircuitBreaker` (`CLOSED` -> `OPEN` -> `HALF_OPEN`) trips
  when failures cross threshold, failing fast with `AgentOutcome.FATAL` rather
  than burning round budget during provider outages. Maps all uncaught exceptions
  into first-class `AgentRunResult` records, satisfying G4.1.
- **Chaos test suite (`tests/test_chaos.py`).** Verifies system resilience under
  model timeouts, tool handler exceptions, auxiliary disk write failures,
  crash/restart with strict event stream prefix invariance, and persistent provider
  outages.
- **`sciteam.event_log`: the institutional ledger.** An append-only,
  hash-chained JSONL event stream (`ledger.jsonl`, one chain per run) frozen by
  `sciteam/data/schemas/institution_event.schema.json`. Every entry records who
  acted (`seat`), under what right (`authority`), whether the act counted
  (`effect`), and against which rule digest it was judged (`rule_version`).
  `verify_chain` detects edits, reordering, deletion and spliced entries, and
  reports every break rather than the first; `replay` derives institutional
  state (effective artifacts, refused acts, vetoes, reopenings, closures,
  amendment status) *from* the record; `cost_report` attributes per-call spend
  to the seat that incurred it. `EventLog` recovers the chain head on reopen, so
  a resumed run extends the same chain. Plugins may append `x_`-prefixed kinds,
  which are chained and verified but carry no interpreter semantics.
- **`sciteam.checkpoint`: crash-safe resume.** `Campaign.run(resume=True)`
  rebuilds planner and budget state by replaying the decisions in the ledger
  and **does not re-execute the missions behind them**. The reason is
  institutional, not economic: a mission whose outcome was accepted produced
  an effect, so re-running it would duplicate or silently replace that effect
  and the record would stop describing what happened. A planner that
  re-proposes a decided mission raises rather than being tolerated. Resume
  extends the same hash chain, so a crash and restart leave one continuous
  verifiable record. Verified end to end against a real `SIGKILL`: the
  resumed run re-executes only the undecided work and lands field-for-field
  on the no-fault baseline. `sciteam ledger resume-state <run>` reports what
  a restart would actually re-run. `Campaign(ledger=...)` is optional;
  without it the campaign behaves exactly as before.
- `MissionOutcome.from_dict`, the counterpart to `to_dict`, so a recorded
  outcome can be restored rather than recomputed.
- **`sciteam.mutation`: mutation testing for institutions.** Breaks a manifest
  on purpose — merge the working and judging seats, misspell an authority,
  duplicate a seat key, remove the exit-artifact duty, declare a negative round
  ceiling — and reports which broken institutions the loader accepted anyway.
  Crucially it also declares *equivalent* mutants (reordering seats, rewriting
  prose, adding an annotation key) that **must survive**: killing one is
  reported as a defect of the same weight as a survivor, so a score of 1.0
  means the loader is strict rather than merely brittle. `sciteam mutate <dir>`
  exits 1 on either. All 21 bundled paradigms score 1.0, enforced in CI.
- **`sciteam.provenance`: environment pinning that admits its own limits.**
  Each run records its build, Python, platform, dependency versions, installed
  plugins and `SCITEAM_*` overrides (credentials redacted, never their values),
  plus a digest over that pinned half — **and an explicit `unpinnable` list**.
  A hosted model endpoint is always reported as uncontrolled, since a vendor
  can change weights behind a stable model id; `reproducible` is therefore
  false unless everything really is pinned. Output-only variables such as
  `SCITEAM_RUNS_ROOT` are recorded outside the digest so that two runs
  differing only in output directory are not flagged as environment drift.
  `sciteam env [--against <run>]` prints the fingerprint or names the covariate
  that moved. Closes the S0.2 gap.
- `sciteam ledger verify | show | replay | cost <run dir | ledger.jsonl>`.
  `verify` exits 1 on a broken chain, so it drops into CI as a guard that a
  run's record was not edited after the fact.
- The canonical demo now emits a real ledger: the unauthorized submission, the
  late audit failure, the authorized veto, the reopen, the repair, the re-audit
  and the legal close are each a verifiable chained entry stamped with the
  manifest digest. `tests/test_canonical_institution_demo.py` pins the exact
  event sequence, and `--config-swap` is visible as a different `rule_version`
  against a byte-identical interpreter.
- `team_loader.parse_team_config(raw, source=...)`: apply every load-time
  institutional invariant to an in-memory manifest, so tools that hold a
  manifest as data get exactly the checks a real load performs instead of a
  reimplementation that can drift from them.
- `sciteam` command-line entry point (`sciteam demo | validate | paradigms list |
  plugins list | ledger | mutate | env | version`). Thin shell over existing
  library functions; adds no behaviour of its own and no `--force`.
- `sciteam.canonical_demo`: the paper's canonical Institution Engineering run
  (and its three structural ablations + `--config-swap`) now ships inside the
  package, so `pip install sciteam && sciteam demo` reproduces it on a machine
  that has never seen this repository. `harness/run_canonical_institution_demo.py`
  remains as a shim.
- `sciteam._paths`: single source of truth for bundled data locations
  (`DATA_ROOT`, `ASSETS_ROOT`, `SCHEMAS_DIR`, `CONTRACTS_DIR`) and for run
  output (`runs_root()`, honouring `SCITEAM_RUNS_ROOT`, defaulting to `./runs`).
- `py.typed` marker; PEP 621 `project.urls`; the `sciteam.plugins` entry-point
  group is declared in `pyproject.toml`.
- `tests/test_cli.py`, including a packaging invariant test that fails if any
  module in `sciteam/` reintroduces a repository-layout assumption.
- Documentation set: `docs/CONCEPTS.md`, `docs/ARCHITECTURE.md`,
  `docs/PLUGIN-GUIDE.md`, `docs/COMPATIBILITY.md`, plus `SECURITY.md`,
  `CONTRIBUTING.md`, and GitHub issue/PR templates.
- Pinned plugin-contract tests (`TestFrozenPluginContract`): `PLUGIN_API_VERSION`,
  the event set, the nine registration methods, `PluginManifest` fields and the
  entry-point group. Changing the plugin surface now requires editing an
  assertion that names it, so it is visible in review.

### Changed
- **Bundled data moved into the package**: `assets/` → `sciteam/data/assets/`,
  `schemas/` → `sciteam/data/schemas/`, `contracts/` → `sciteam/data/contracts/`.
  Before this change a non-editable install could not load any manifest, prompt,
  skill or schema (every module resolved them relative to a repository checkout).
  Code that passed explicit paths (`build_registry(root)`, `ContractValidator(dir)`,
  `PromptAssets(...)`, `SCITEAM_ASSETS_ROOT`) is unaffected; code that relied on
  the repository layout should import from `sciteam._paths`.
- The canonical demo's frozen tests run under a stdlib-only runner instead of
  `pytest`, removing an undeclared runtime dependency on a test framework.
- `INTERPRETER_FILES` (the hashed "interpreter" of the canonical run) are now
  package-relative; the E0 check is identical for a source checkout and a wheel.
- Operator-facing strings in `budget_gate.py`, `registry_catalog.py` and
  `audit_veto.py` are now English (they appear in run artifacts and error
  messages).
- Failures that silently discarded evidence (a run artifact, seat memory or a
  team-run snapshot that could not be written) now log a warning through the
  standard `logging` module instead of `except OSError: pass`. The control flow
  is unchanged — these were always best-effort writes — but an operator can now
  see that a record was lost.
- **Module splits** along real seams, no behaviour change: `llm_worker.py`
  1731 → 1066 lines, with prompt assembly in `sciteam/prompt_assets.py`
  (envelope instructions + `PromptAssets`) and the LLM↔tool inner loop in
  `sciteam/tool_loop.py` (`ToolLoopMixin`); `mission.py` 1039 → 798, with the
  mission spec, budget, `ContractValidator` and outcome record in
  `sciteam/mission_contracts.py`. Every moved name is still importable from
  its original module.
- `sciteam/arm_identity.py` → `sciteam/runtime_identity.py`, generalized out
  of the research tree's arm vocabulary: `describe_own_arm` →
  `describe_local_runtime` (now `"local ..."`, with `SCITEAM_IMAGE_DIGEST`
  pinning an installed deployment), and `arm_version(arm, runtime)` →
  `runtime_version(runtime)`, which asks any backend exposing `version()`
  and records `"unknown"` rather than misattributing a backend whose
  version lookup fails.
- Version bumped to `0.2.0.dev0`.

### Removed
- `sciteam/science_reviser.py`. An audit for dead modules found it was the
  only file in the package with no importer, no export and no test — a
  research-tree leftover. Removed rather than renamed (the previously
  planned action): shipping an untested, unreachable module in a base whose
  promise is stability is worse than the name.

### Fixed
- **Two fail-open gaps in the institution loader, both found by `sciteam mutate`
  on its first run.** A manifest in which *no* seat holds `emits_exit_artifact`
  used to load cleanly and could then never be closed legally; it is now
  rejected at load. A negative `stopping.max_iterations` used to be clamped to
  0 — which means *unlimited* rounds — silently turning a typo into the
  opposite of what it said; it is now rejected, while an explicit 0 keeps its
  legitimate "no ceiling" meaning.
- The `clean-install` CI job was declared twice in `ci.yml`; YAML kept only the
  second, so the first never ran. Now one job, extended to verify the canonical
  run's ledger, confirm a tampered ledger fails verification, and require every
  bundled institution to survive no non-equivalent mutant.
- `wizard_tools.amendment_audit` without an explicit `lab_root` now resolves
  schemas/assets from the bundled data and `failure_refs` against the working
  directory (previously both were resolved against the package's parent).

## [0.1.0] — 2026-09-06

Initial public release: institution manifests, authority check, veto →
mechanical reopen, behavioral shadow, amendment + rollback, `RuntimePort`,
plugin engine (`sciteam.plugin_api`, P1–P3), 21 coordination paradigm
templates, canonical demo + ablations, 376 tests.
