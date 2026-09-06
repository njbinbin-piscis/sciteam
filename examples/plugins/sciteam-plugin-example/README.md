# sciteam-plugin-example

Reference plugin for `sciteam`'s plugin engine
(`sciteam/plugin_api.py`, design: `docs/25-sciteam-plugin-architecture.md`
in the companion research repo). It exists to be read, not to be useful —
every extension point it touches is the minimum needed to prove the point:

| What it registers | How |
|---|---|
| A new team paradigm, `round_robin_review` | `api.register_paradigm_dir(...)` — one extra YAML file, discovered by `sciteam.team_loader.build_registry(base, extra_roots=[...])` exactly like a hand-written one |
| A new tool, `echo` | `api.register_tool(ToolSpec(name="echo", ...), handler=...)` — reachable via `sciteam.wizard_tools.run_wizard_tool("echo", args, plugins=registry)` |
| A `tool_call` observer | `api.on("tool_call", ...)` — increments a counter; returns `None` (observe-only, never blocks) |

None of this required a single line changed in `sciteam/`.

## Install

```bash
# from the sciteam repo root — sciteam itself must already be installed
pip install -e .
pip install -e examples/plugins/sciteam-plugin-example
```

## Discover and use it

```python
from sciteam.plugin_api import load_all_plugins
from sciteam.team_loader import build_registry
from sciteam.wizard_tools import run_wizard_tool

registry = load_all_plugins(strict=True)
print([m.id for m in registry.loaded_plugins])  # ['sciteam-plugin-example']

teams = build_registry(
    "assets/teams/paradigms",
    extra_roots=registry.paradigm_dirs,
)
assert teams.get("round_robin_review") is not None

result = run_wizard_tool("echo", {"hello": "world"}, plugins=registry)
assert result == {"ok": True, "echo": {"hello": "world"}}
```

## Run this plugin's own tests

```bash
cd examples/plugins/sciteam-plugin-example
pip install -e ".[dev]"
python3 -m pytest -q
```

## Writing your own plugin

Copy this directory, rename the package and the entry-point key under
`[project.entry-points."sciteam.plugins"]` in `pyproject.toml`, and replace
`register()` in `src/sciteam_plugin_example/__init__.py`. The full contract
— every event a hook can subscribe to, which two are blockable and why,
the version-compatibility rule, and what a plugin is explicitly *not*
allowed to do (override a builtin, sandbox escape, hot reload) — is in
`docs/25-sciteam-plugin-architecture.md`.
