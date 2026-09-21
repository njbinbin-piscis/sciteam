# SciTeam

[![CI](https://github.com/njbinbin-piscis/sciteam/actions/workflows/ci.yml/badge.svg)](https://github.com/njbinbin-piscis/sciteam/actions/workflows/ci.yml)

**A small runtime for treating multi-agent coordination as an *institution*,
not a prompt.**

Most multi-agent frameworks give every agent the same kind of thing: a
role description in a system prompt, and a graph or a loop that calls them
in some order. Whether an agent's output actually *counts* — whether it was
allowed to submit, whether a rejection can be appealed, whether the rule
that just fired can itself be changed later — usually isn't a first-class
concept. It's either hard-coded into the orchestrator or trusted to the
prompt.

SciTeam makes that machinery explicit and executable:

- agents occupy **seats** in an **institution** (a YAML manifest), not
  just characters in a prompt;
- a seat's output only gains system-level effect if the institution's
  rules say so — **authority is checked in code, not assumed from the
  prompt**;
- a rejected submission can trigger a **mechanical reopen** (veto →
  re-entry), not just a retry;
- a bad rule change can be **mechanically rolled back** from an
  append-only ledger;
- switching coordination pattern (single approver → committee, chain →
  fan-out, etc.) is a **manifest edit**, not an orchestrator patch.

This is the reference runtime built alongside the paper *"Institution
Engineering: [title TBD]"* (arXiv, forthcoming). The paper argues the
general case; this repository is the existence proof you can run.

## 60-second quickstart (no API key required)

```bash
pip install sciteam        # or: pipx install sciteam
sciteam demo --mode full
```

(From a source checkout: `pip install -e ".[dev]"` and either `sciteam demo`
or `python harness/run_canonical_institution_demo.py --mode full` — the
script is a shim over the same `sciteam.canonical_demo` module.)

This runs a small, real repo-level bug-fix task through six declared
seats (implementer, auditor, chair — plus a bystander with no seat at
all) using deterministic scripted agents, so it costs nothing and needs
no LLM credentials. It is the exact run cited in the paper. Real output,
unedited:

```text
"implementer(attempt=1): wrote patch, submission authorized (seat=implementer)",
"auditor(attempt=1): frozen tests passed=False",
"auditor: wrote authorized veto targeting m_implement",
"implementer(attempt=2): wrote patch, submission authorized (seat=implementer)",
"auditor(attempt=2): frozen tests passed=True",
"chair: closed_legally=True (approvals={'auditor': True}, rule={'kind': 'single', 'roles': ['auditor'], 'threshold': 1})"
```

Three things happened that a plain prompt-and-loop harness does not give
you for free:

1. **Authority was enforced, not assumed.** Only the `implementer` seat
   may write the exit artifact; a bystander's identical patch would be
   rejected before it ever reaches the auditor
   (`sciteam/worker_common.py::is_artifact_role`).
2. **A failed audit produced a legal reopen, not a silent retry.** The
   auditor's veto is itself an authorized act (`may_veto_exit`), and it
   mechanically drives `AdaptiveCampaignPlanner.reopen_upstream` — the
   same code path a unit test pins down in isolation
   (`tests/test_audit_veto.py`).
3. **The interpreter never changed.** `interpreter_hash_before ==
   interpreter_hash_after` for every mode and for `--config-swap`. All of
   the above is driven by ~6 files of general-purpose institution code
   plus one YAML manifest — nothing about *this* task is hard-coded into
   the engine.

And none of it has to be taken on trust. The run leaves a hash-chained
ledger you can check yourself:

```console
$ sciteam ledger show ./runs/canonical_institution_demo/full
   3 x artifact_submitted   bystander      [may_emit_artifact]
   4 + artifact_accepted    implementer    [may_emit_artifact]
   5 + audit_reported       auditor        [may_assess_round]
   6 + veto_issued          auditor        [may_veto]
   7 + reopened             planner        [may_revise_plan]
   8 + artifact_accepted    implementer    [may_emit_artifact]
  10 + closed               chair          [may_close]

$ sciteam ledger verify ./runs/canonical_institution_demo/full
ok    .../ledger.jsonl  11 entries, head e05565d7c96a073e…
```

The `x` on entry 3 is the argument in one character: the bystander's patch
physically exists, and the record says it never counted. Edit, reorder or
delete any line and `sciteam ledger verify` exits non-zero and names the
break — which is what makes it evidence rather than a log.

### See why the guardrails matter: run the ablations

```bash
sciteam demo --mode prompt_only
sciteam demo --mode no_separation
sciteam demo --mode no_recovery
```

- `prompt_only` (no machine-checked tests, chair accepts the
  implementer's own "done" narrative) **closes `closed_legally=True` —
  while the bug is still in the file.** A textbook false positive: without
  a mechanical validity check, self-report is indistinguishable from
  truth.
- `no_separation` (implementer doubles as auditor) also closes
  `True` on the still-buggy patch — separation of powers isn't
  decoration, removing it produces the same false positive by a
  different route.
- `no_recovery` (no legal reopen path) leaves the failed audit
  permanently unresolved (`closed_legally=False`, no way forward) —
  the flip side: without a repair path, a legitimate rejection is a
  dead end, not a corrective loop.

### Change the rule, not the code

```bash
sciteam demo --mode full --config-swap
```

Switches "one auditor approves" to "two-auditor committee, both must
approve." Only the institution manifest changes
(`approval_rule: {kind: committee, roles: [auditor_1, auditor_2],
threshold: 2}`); `interpreter_hash_unchanged` is still `true`. This is
the whole point: **coordination pattern is data your institution owns,
not logic your engine owns.**

## Core mechanics

| Mechanic | What it does | Where |
|---|---|---|
| Institution manifest | Declares seats, authority (`emits_exit_artifact`, `may_veto_exit`, `may_assess_round`, ...), coordination topology/scheduling/stopping rule | `sciteam/team_loader.py`, `sciteam/coordination.py` |
| Authority check | An artifact only counts if the emitting seat is authorized | `sciteam/worker_common.py` (`is_artifact_role`), `sciteam/exit_artifact.py` |
| Veto → mechanical reopen | An authorized veto re-enters the DAG at the vetoed node, not a hand-rolled retry | `sciteam/audit_veto.py`, `sciteam/adaptive_planner.py` |
| Behavioral shadow | Simulate a candidate rule change against recorded authority-relevant events before promoting it | `sciteam/behavioral_shadow.py` |
| Amendment + rollback | Institution changes go through a status state machine and are revertible from archived before/after pairs (refusing on drift) | `sciteam/amendment.py` |
| Institutional ledger | Append-only, hash-chained record of who acted, under what authority, whether it counted, and under which rule version — with tamper detection, replay and per-seat cost attribution | `sciteam/event_log.py`, `sciteam ledger verify\|show\|replay\|cost` |
| Crash-safe resume | A killed run restarts from its ledger: decided missions are replayed into the planner and clock, never re-executed, and the resumed run extends the same hash chain | `sciteam/checkpoint.py`, `Campaign.run(resume=True)`, `sciteam ledger resume-state` |
| Mutation testing | Break your institution on purpose (merge work and judgment, misspell an authority, duplicate a seat key) and check the loader notices — with declared *equivalent* mutants that must survive, so a score of 1.0 means strictness, not brittleness | `sciteam/mutation.py`, `sciteam mutate` |
| Provenance | Every run records what it pinned (build, Python, dependencies, plugins, `SCITEAM_*` overrides with credentials redacted) **and what it could not** — a hosted model endpoint is always reported as uncontrolled rather than silently assumed reproducible | `sciteam/provenance.py`, `sciteam env` |
| `RuntimePort` resilience | Composable timeout, exponential backoff with jitter, and stateful circuit breaker for remote backends — all failures map to first-class `AgentOutcome` states rather than uncaught crashes | `sciteam/resilience.py`, `ResilientRuntimePort` |
| `RuntimePort` | The one seam between "institution" and "model" — plug in any LLM/agent backend | `sciteam/runtime.py`, `sciteam/llm_worker.py` |
| Scaffolding | Generate runnable, testable institutional MAS projects from 21 bundled paradigms | `sciteam/scaffold.py`, `sciteam init` |
| Ledger view | Export interactive, self-contained HTML trajectories with zero external CDN dependencies | `sciteam/ledger_view.py`, `sciteam ledger view` |
| Plan revision policy | How the campaign DAG grows/reopens in response to outcomes is itself a pluggable policy, not engine logic | `sciteam/adaptive_planner.py` (`PlanReviser`, `CompositeReviser`) |
| Plugin engine | Discover/register third-party runtimes, tools, domain ports, paradigms, and institutional event hooks — without editing `sciteam/` | `sciteam/plugin_api.py` (see "Extending SciTeam" below) |

## Command line

```text
sciteam init       <template> [-t DIR] [-n NAME] [--list]  # scaffold runnable institutional project
sciteam demo       [--mode full|prompt_only|no_separation|no_recovery] [--config-swap] [--out DIR]
sciteam validate   <manifest.yaml | dir> ...   # load-time institutional invariants, exit 1 on any failure
sciteam paradigms  list [--with-plugins]       # bundled coordination templates
sciteam plugins    list [--strict] [--json]    # discovered entry-point plugins
sciteam ledger     verify|show|replay|cost|resume-state|view <run dir | ledger.jsonl>
sciteam mutate     <manifest.yaml | dir> [-v]  # break institutions on purpose, check the loader notices
sciteam env        [--against <run dir>]       # provenance fingerprint, and what it cannot pin
sciteam version    [--json]
```

The CLI is a thin shell over library functions the tests already cover; it
adds no behaviour and has no `--force`. Run output goes under `./runs/` (or
`$SCITEAM_RUNS_ROOT`), never into the installed package. All runtime data
(manifests, prompts, skills, schemas) ships inside the wheel under
`sciteam/data/`, so a plain `pip install` is self-sufficient — see
`sciteam/_paths.py`.

## Bring your own model

```bash
export SCITEAM_LLM_BASE_URL=https://api.your-provider.com/v1
export SCITEAM_LLM_API_KEY=...
export SCITEAM_LLM_MODEL=your-model-id
.venv/bin/python harness/run_campaign.py --profile open --run-name my_run \
  --goal "..."
```

`sciteam/llm.py` is a stdlib-only, OpenAI-compatible client with zero
framework dependencies; `sciteam/llm_worker.py` is the reference
`RuntimePort` implementation. Implement `RuntimePort` yourself to plug in
any other backend — the institution layer does not know or care which
model is behind a seat.

## A minimal institution, end to end

```yaml
id: two_seat_review
name: Two-Seat Review
members:
  - key: implementer
    role: implementer
    emits_exit_artifact: true
  - key: auditor
    role: auditor
    authority: [may_veto_exit]
  - key: chair
    role: chair
coordination:
  topology: chain
  scheduling: { mode: barrier, max_parallel: 1 }
  stopping: { kind: judgment, max_iterations: 20 }
```

Load it with `sciteam.team_loader.load_team_configs`, run it with
`sciteam.orchestrator.TeamOrchestrator` or the higher-level
`sciteam.campaign.Campaign`. See `sciteam/data/assets/teams/paradigms/`
(`sciteam paradigms list`) for 21
ready-made coordination templates (adversarial pair, debate + Elo,
fan-out/gather, generator/critic, pipeline chain, planning council,
retro council, standing watch, ...) — each is a manifest, none require
touching engine code.

## Extending SciTeam without touching the base

`sciteam/` is meant to stop moving once its contracts are frozen; new
backends, tools, domain integrations, and coordination templates are meant
to arrive as **plugins** — ordinary, separately-installable Python packages
discovered via standard `importlib.metadata` entry points (the same
mechanism `pytest`/`coverage.py`/SQLAlchemy dialects use), never as PRs
against this repo. Full guide: [`docs/PLUGIN-GUIDE.md`](docs/PLUGIN-GUIDE.md).

A plugin is one function:

```python
# my_plugin/__init__.py
from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginManifest
from sciteam.tool_catalog import ToolSpec

def register(api: PluginAPI) -> PluginManifest:
    api.register_tool(
        ToolSpec(name="patent_search", version="1.0", capability="patent.search", summary="..."),
        handler=my_patent_search_handler,
    )
    api.register_paradigm_dir(my_paradigms_dir)      # new coordination templates
    api.on("tool_call", my_audit_hook)                # observe, or veto (see docs)
    return PluginManifest(id="my-plugin", version="1.0", sciteam_api_version=PLUGIN_API_VERSION)
```

```toml
# my_plugin's own pyproject.toml
[project.entry-points."sciteam.plugins"]
my_plugin = "my_plugin:register"
```

`pip install my-plugin` and it is discovered automatically by
`sciteam.plugin_api.load_all_plugins()` — never as an import side effect of
`import sciteam` itself (a composition root calls this explicitly). Rules
worth knowing before you write one:

- **Plugins add, they never override.** A plugin cannot shadow a built-in
  tool, paradigm, or role prompt — name conflicts are a hard, fail-closed
  error at load time, not a silent "last one wins".
- **Two hooks can veto, the rest only observe.** `tool_call` and
  `artifact_submitted` are the two points where "does this action take
  effect" is actually decided; every other lifecycle event (`mission_start`,
  `round_end`, `veto_issued`, `amendment_applied`) already has its own
  authority mechanism elsewhere and a plugin only gets to watch it.
- **`PLUGIN_API_VERSION` is frozen independently of `sciteam.__version__`.**
  Internal refactors that don't touch the registration surface never bump
  it; a plugin targeting an incompatible major version is skipped
  (fail-closed, per-plugin) rather than crashing the whole process.

A complete, runnable reference plugin — one paradigm, one tool, one
observing hook — lives in
[`examples/plugins/sciteam-plugin-example/`](examples/plugins/sciteam-plugin-example/):

```bash
pip install -e .
pip install -e examples/plugins/sciteam-plugin-example
python3 -c "from sciteam.plugin_api import load_all_plugins; \
             print([m.id for m in load_all_plugins(strict=True).loaded_plugins])"
```

### Official provider plugins

SciTeam maintains official, zero-core-dependency plugins under [`plugins/`](plugins/):

* [`sciteam-runtime-anthropic`](plugins/sciteam-runtime-anthropic): Anthropic Claude Messages API adapter (`claude-3-5-sonnet`, `claude-3-7-sonnet`), mapping native tool-use and error dispositions (401 fatal, 429/529 recoverable).
* [`sciteam-runtime-deepseek`](plugins/sciteam-runtime-deepseek): DeepSeek API adapter (`deepseek-chat`, `deepseek-reasoner` / R1), automatically isolating chain-of-thought into `RunSpec.metadata["reasoning_content"]` for auditing without corrupting exit artifacts.
* [`sciteam-tool-mcp`](plugins/sciteam-tool-mcp): Model Context Protocol (MCP) tool bridge adapting external MCP tools into governed `ToolSpec` instances, protected by pre-execution `tool_call` hooks.
* [`sciteam-sandbox-docker`](plugins/sciteam-sandbox-docker): Docker container sandbox implementing `SandboxPort` (`run` and `certify`), executing programs under hermetic Linux container isolation driven by manifest `guard` declarations.

### Production reference deployments

SciTeam provides complete reference deployments under [`examples/`](examples/):

* [`examples/github_pr_review`](examples/github_pr_review): GitHub PR Review and Merge Control institution with separation of powers between author, quality reviewer, security scanner, and release assembler, enforcing committee voting and authorized security vetoes.
* [`examples/research_lab_tournament`](examples/research_lab_tournament): Autonomous Research Lab hypothesis tournament with generation, skeptical falsifiability critique, blind pairwise ranking, literature proximity novelty auditing, and consensus meta-review.

## What this is not

- Not a benchmark. There is no leaderboard here, and the canonical demo
  is deliberately a small, legible task, not a hard one.
- Not a claim that any specific coordination *topology* is novel — chain,
  fan-out, debate, etc. are standard patterns. The contribution is
  making the *authority layer around them* (who may act, what counts,
  how rules change) a first-class, executable, auditable artifact.
- The shipped canonical demo uses scripted (deterministic) seats, not
  live LLM calls, so it costs nothing and needs no credentials to
  reproduce the paper's cited run. Live-LLM campaigns are fully
  supported (`run_campaign.py` + your own `SCITEAM_LLM_*` credentials)
  but are a separate, budget-gated exercise — see the paper's
  reproducibility section for what was actually run with real models
  and what remains future work.
- Pre-1.0: the public API (`sciteam.__all__`) and CLI follow semver from
  0.2.0 onward (see `CHANGELOG.md`); breaking changes bump the minor
  version until 1.0. `PLUGIN_API_VERSION` is versioned separately.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/CONCEPTS.md`](docs/CONCEPTS.md) | seats, authority, counts-as, recovery, amendment, ledger — the six terms everything else is built from |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | module map, dependency direction, what a run actually does |
| [`docs/PARADIGMS-GUIDE.md`](docs/PARADIGMS-GUIDE.md) | 21 built-in coordination paradigms and when-to-use decision matrix |
| [`docs/PLUGIN-GUIDE.md`](docs/PLUGIN-GUIDE.md) | writing a plugin; the four rules and why they exist |
| [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) | what semver covers, and why `PLUGIN_API_VERSION` is separate |
| [`SECURITY.md`](SECURITY.md) | trust model, sandbox limits, what this explicitly does not defend against |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | plugin-or-PR decision table, conventions, local checks |
| [`CHANGELOG.md`](CHANGELOG.md) | release history |

## Status

- 572 tests passing, 4 skipped, 0 failing on this export's test subset
  (`ruff check` / `ruff format --check` / `pytest` all clean in CI — see
  the badge above), plus 4 official provider plugin packages under `plugins/`
  and an example plugin package (`examples/plugins/sciteam-plugin-example/`).
- MIT licensed.
- Companion paper: *"Institution Engineering: [title TBD]"* (arXiv,
  forthcoming) — link will be added here on submission.

## License

MIT — see `LICENSE`.
