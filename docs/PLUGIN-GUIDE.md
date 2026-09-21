# Plugin guide

`sciteam/` is meant to stop moving once its contracts are frozen. New model
backends, tools, domain integrations and coordination templates arrive as
**plugins**: ordinary, separately-installable Python packages discovered
through standard `importlib.metadata` entry points — the same mechanism
`pytest`, `coverage.py` and SQLAlchemy dialects use. Never as a pull request
against this repository.

## A plugin is one function

```python
# my_plugin/__init__.py
from pathlib import Path

from sciteam.plugin_api import PLUGIN_API_VERSION, HookResult, PluginAPI, PluginManifest
from sciteam.tool_catalog import ToolSpec

PARADIGMS = Path(__file__).parent / "paradigms"


def register(api: PluginAPI) -> PluginManifest:
    api.register_tool(
        ToolSpec(
            name="patent_search",
            version="1.0",
            capability="patent.search",
            summary="Search a patent database",
            permission="readonly",
        ),
        handler=_patent_search,
    )
    api.register_paradigm_dir(PARADIGMS)
    api.on("tool_call", _audit_hook)
    return PluginManifest(
        id="sciteam-plugin-patents",
        version="1.0",
        sciteam_api_version=PLUGIN_API_VERSION,
        provides=("tool", "paradigm", "hook"),
        description="Patent search for prior-art seats",
    )
```

```toml
# my_plugin's own pyproject.toml
[project.entry-points."sciteam.plugins"]
my_plugin = "my_plugin:register"
```

`pip install my-plugin` and it is discovered. A complete runnable reference —
one paradigm, one tool, one hook, with its own tests and CI job — lives in
[`examples/plugins/sciteam-plugin-example/`](../examples/plugins/sciteam-plugin-example/).

## Loading is explicit, never an import side effect

```python
from sciteam.plugin_api import load_all_plugins

registry = load_all_plugins()          # in your composition root
registry = load_all_plugins(strict=True)   # in CI: fail on the first bad plugin
```

`import sciteam` never loads plugins. A unit test that did not ask for
plugins must not change behaviour because some unrelated package happens to
be installed in the same environment. Inspect what is installed with
`sciteam plugins list [--json]`.

## The registration surface

| Method | Registers |
|---|---|
| `register_runtime_port(name, factory)` | a model/agent backend implementing `RuntimePort` |
| `register_tool(spec, handler)` | a tool callable from a seat's tool loop |
| `register_port(kind, name, factory)` | a `compute` / `dataset` / `literature` / `verifier` implementation |
| `register_plan_reviser(name, reviser)` | a `PlanReviser` policy for how the plan DAG reacts to outcomes |
| `register_assessor(name, factory)` | a round-assessment implementation |
| `register_paradigm_dir(path)` | a directory of institution manifests |
| `register_role_prompts_dir(path)` | a directory of role prompts |
| `register_skills_dir(path)` | a directory of skill definitions |
| `on(event, handler)` | an institutional lifecycle hook |

Asset directories are *additional search paths* for the loaders that already
exist — `build_registry(root, extra_roots=registry.paradigm_dirs)`,
`PromptAssets(..., extra_prompts_dirs=..., extra_skills_dirs=...)`. There is
no second parser and no second validation path: a plugin's manifests go
through exactly the same load-time invariants as the bundled ones.

## Events

| Event | Fires | Can block |
|---|---|---|
| `mission_start` | before a mission is dispatched | no |
| `round_end` | after a coordination round is assessed | no |
| `tool_call` | before a tool call executes | **yes** |
| `artifact_submitted` | after schema validation, before the exit artifact lands | **yes** |
| `veto_issued` | after a veto signal is processed | no |
| `amendment_applied` | after an amendment (or rollback) completes | no |

A handler returns `None` to observe, or `HookResult(block=True, reason=...)`
to veto — only for the two blockable events.

```python
def _audit_hook(payload: dict) -> HookResult | None:
    if payload.get("tool") == "shell" and "rm -rf" in payload.get("arguments", {}).get("cmd", ""):
        return HookResult(block=True, reason="destructive shell command")
    return None
```

Every handler runs, in registration order, so every plugin observes the event
regardless of an earlier block. The **first** `block=True` wins: a later
handler cannot un-block what an earlier one vetoed. A handler that raises is
logged and treated as "no opinion" — one broken hook must not take down a
mission.

## Four rules, and why they exist

These are not politeness; they follow from what the project is about. A
plugin system has to answer the same question an institution does: *who may
register what, does it pass the existing authority checks, and can an
extension point overrule a frozen ruling?*

**1. Plugins add, they never override.** A plugin cannot shadow a built-in
tool, paradigm, role prompt or port. A name collision is a hard, fail-closed
error at load time, not a silent "last one wins". Otherwise the effective
behaviour of a system would depend on plugin installation order — a hidden
precedence war with no institutional basis.

**2. Only two events can block, and only ever to add scrutiny.**
`tool_call` and `artifact_submitted` are the points where "does this action
take effect" is actually decided. Every other event already has its own
authority mechanism (declarative seat authority in `team_loader`, the
amendment layer whitelist, `audit_veto`'s RBAC). A plugin does not get a
second opinion on a ruling another mechanism already made. Plugins add
constraints, never exemptions.

**3. Plugins decide *who runs*; the institution decides *what counts*.**
`is_artifact_role` honours only the declarative authority parsed from the
manifest. It does not care whether the seat is backed by the bundled
`LlmWorkerRuntime` or a `RuntimePort` from your plugin. This is the boundary
that separates this design from ordinary backend pluggability.

**4. Failure is isolated per plugin.** With `strict=False` (the default) a
plugin that fails to import, returns the wrong type, duplicates an id or
targets an incompatible API version is skipped with a warning; everything
else still loads. A plugin's registrations are staged privately and merged
only after its manifest validates, so a rejected plugin never leaves partial
state behind. `strict=True` is for CI and pre-release verification.

## Versioning

`PLUGIN_API_VERSION` (currently `"1.0"`) is frozen **independently of**
`sciteam.__version__`. Internal refactors that do not touch the registration
surface never bump it. A plugin declaring an incompatible major version is
skipped rather than crashing the process. See `docs/COMPATIBILITY.md` for
what exactly is covered and the process for changing it.

## Deliberate non-goals

- **No plugin marketplace.** Distribution is PyPI or your private index.
- **No sandboxing.** Plugins are trusted in-process code, the same trust
  model as `pytest` plugins. See `SECURITY.md`.
- **No hot reload.** Plugins are discovered once at process start. A
  multi-hour campaign is not an IDE.
- **No override mechanism.** See rule 1.
