"""Reference SciTeam plugin (docs/25-sciteam-plugin-architecture.md, P3).

Demonstrates the three concrete plugin extension points wired in P1/P2 —
without a single line of ``sciteam/`` itself needing to change for any of
them:

1. A new team paradigm (``paradigms/round_robin_review.yaml``), registered
   via ``api.register_paradigm_dir`` — picked up wherever a composition root
   calls ``sciteam.team_loader.build_registry(base, extra_roots=registry.paradigm_dirs)``.
2. A new tool, ``echo`` (pure demonstration — echoes its arguments back with
   no side effects), registered via ``api.register_tool`` — reachable
   through ``sciteam.wizard_tools.run_wizard_tool(name, args, plugins=registry)``.
3. A ``tool_call`` hook that counts calls, registered via ``api.on`` —
   observe-only (returns ``None``, never blocks); see
   ``sciteam.plugin_api.EVENTS``/``BLOCKABLE_EVENTS`` for the full event
   table and which two events a hook may veto instead of just observe.

Install and discover::

    pip install -e .                                    # from repo root
    pip install -e examples/plugins/sciteam-plugin-example
    python3 -c "from sciteam.plugin_api import load_all_plugins; \
                 r = load_all_plugins(strict=True); \
                 print([m.id for m in r.loaded_plugins])"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sciteam.plugin_api import PLUGIN_API_VERSION, HookResult, PluginAPI, PluginManifest
from sciteam.tool_catalog import ToolSpec

PARADIGMS_DIR = Path(__file__).resolve().parent / "paradigms"

#: Process-lifetime counter the ``tool_call`` hook below increments.
#: Exposed as plain module state (not hidden in a closure) so this
#: plugin's own tests can assert on it directly — a production hook would
#: more plausibly emit to a metrics/log sink than accumulate in memory.
tool_call_count = 0


def reset_tool_call_count() -> None:
    """Test helper — the counter is module-global (see ``tool_call_count``),
    so tests that care about an exact count reset it first."""
    global tool_call_count
    tool_call_count = 0


def echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "echo": arguments}


def _count_tool_calls(payload: dict[str, Any]) -> HookResult | None:
    global tool_call_count
    tool_call_count += 1
    return None  # observe only — this hook never blocks (see module docstring)


def register(api: PluginAPI) -> PluginManifest:
    api.register_paradigm_dir(PARADIGMS_DIR)
    api.register_tool(
        ToolSpec(
            name="echo",
            version="1.0",
            capability="example.echo",
            summary="Echoes its arguments back. Pure demonstration, no side effects.",
            permission="readonly",
        ),
        handler=echo,
    )
    api.on("tool_call", _count_tool_calls)
    return PluginManifest(
        id="sciteam-plugin-example",
        version="0.1.0",
        sciteam_api_version=PLUGIN_API_VERSION,
        provides=("paradigm", "tool", "hook"),
        description=(
            "Reference plugin: round_robin_review paradigm + echo tool + tool_call counter."
        ),
    )
