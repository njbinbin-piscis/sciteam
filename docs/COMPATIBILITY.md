# Compatibility policy

Two version numbers, deliberately independent.

## `sciteam.__version__` — the package

Semantic versioning from `0.2.0` onward. Pre-1.0, a **breaking change bumps
the minor version**; patch releases are strictly backward compatible.

Covered by this promise:

- everything exported from `sciteam.__all__`;
- the `sciteam` CLI: subcommand names, their flags, and their exit codes;
- the institution manifest format (seat keys, `authority`, `emits_exit_artifact`,
  `coordination.*`) and the load-time invariants applied to it;
- the shape of `summary.json` produced by `sciteam demo`;
- the environment variables `SCITEAM_ASSETS_ROOT`, `SCITEAM_RUNS_ROOT`,
  `SCITEAM_LLM_BASE_URL`, `SCITEAM_LLM_API_KEY`, `SCITEAM_LLM_MODEL`.

Not covered — change freely, at any version:

- anything under a leading underscore (`sciteam._paths`, `_ToolRegistration`,
  private methods on `PluginRegistry`);
- how a module implements a covered contract (`llm_worker.py` internals,
  `mission.py` scheduling details), including splitting a module into
  several, as long as the public names keep resolving;
- log message wording, `runs/` directory layout beyond documented artifacts,
  and the bundled asset content (new paradigms and prompts may be added).

Every user-visible change is recorded in `CHANGELOG.md` before release.

## `PLUGIN_API_VERSION` — the plugin surface

`sciteam.plugin_api.PLUGIN_API_VERSION` is currently `"1.0"`. Compatibility
is by **major component**: a plugin declaring `"1.4"` loads against `"1.0"`
and vice versa; one declaring `"2.0"` is skipped, fail-closed, per plugin.

Frozen under this number:

- the nine registration methods on `PluginAPI` and their signatures;
- the event names in `EVENTS`, which of them are in `BLOCKABLE_EVENTS`, and
  the `HookResult` return shape;
- the fields of `PluginManifest`;
- the entry-point group name `sciteam.plugins`;
- the Protocols a plugin implements against: `RuntimePort`, `ToolSpec`,
  `PlanReviser`, `ComputePort`, `DatasetPort`, `LiteraturePort`,
  `VerifierPort`.

**Why it is separate.** "The base stops moving" is a promise about
*interfaces*, not about *implementations*. Refactoring `llm_worker.py`,
rewriting the scheduler, or renaming an internal module must not force every
plugin author to re-release. Conversely, a change to the registration surface
is breaking even if no line of `sciteam/`'s behaviour changed.

### Changing it

Any change to the frozen list above requires all three:

1. bump the **major** component of `PLUGIN_API_VERSION`;
2. a `CHANGELOG.md` entry with a migration path;
3. update the pinned assertion in the plugin API contract test.

The pinned assertion exists for exactly one reason: it forces any pull
request that touches the registration surface to also edit a line that says
what the version is, so the change is visible in review instead of arriving
silently.

Adding a *new* registration method or a *new* non-blocking event is additive
and bumps the minor component only — existing plugins keep working
unchanged.

## Python and platforms

Supported: CPython 3.11, 3.12, 3.13, on Linux and macOS. CI runs the full
test suite on all three versions and additionally verifies, on Linux and
macOS, that a freshly built wheel installed into a clean virtualenv (with no
dev dependencies and no repository checkout on `sys.path`) reproduces the
canonical run's truth table.

Runtime dependencies are `pyyaml` and `jsonschema`. Adding a third is a
deliberate decision, not a convenience — the point of the LLM client being
~200 lines of stdlib is that adopting SciTeam does not drag a framework into
your dependency tree.

## Deprecation

A covered name that is going away is kept working for at least one minor
release, emits a `DeprecationWarning` pointing at its replacement, and is
listed under `Deprecated` in `CHANGELOG.md` for that release. Removals
happen only in the release that bumps the version according to the rules
above.
