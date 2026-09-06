"""sciteam-plugin-example's own tests.

Exercises the plugin two ways: (a) directly, calling `register()` against a
fresh `PluginRegistry` (no discovery involved), and (b) end to end, through
`sciteam.plugin_api.load_all_plugins()` — which only finds this plugin if it
was actually `pip install`-ed with its `sciteam.plugins` entry point
registered, so a green (b) test is itself evidence the packaging is
correct, not just the Python.
"""

from __future__ import annotations

import sciteam_plugin_example as plugin_module
from sciteam.plugin_api import PluginAPI, PluginRegistry, load_all_plugins
from sciteam.team_loader import build_registry
from sciteam.wizard_tools import run_wizard_tool


def test_register_direct():
    registry = PluginRegistry()
    manifest = plugin_module.register(PluginAPI("sciteam-plugin-example", registry))

    assert manifest.id == "sciteam-plugin-example"
    assert set(manifest.provides) == {"paradigm", "tool", "hook"}
    assert registry.tool_handler("echo") is not None
    assert registry.paradigm_dirs == (plugin_module.PARADIGMS_DIR,)


def test_echo_tool_via_run_wizard_tool():
    registry = PluginRegistry()
    plugin_module.register(PluginAPI("sciteam-plugin-example", registry))

    result = run_wizard_tool("echo", {"hello": "world"}, plugins=registry)
    assert result == {"ok": True, "echo": {"hello": "world"}}


def test_paradigm_dir_is_discoverable_by_build_registry(tmp_path):
    registry = PluginRegistry()
    plugin_module.register(PluginAPI("sciteam-plugin-example", registry))

    empty_base = tmp_path / "no_builtin_paradigms_here"
    empty_base.mkdir()
    teams = build_registry(empty_base, extra_roots=registry.paradigm_dirs)
    cfg = teams.get("round_robin_review")
    assert cfg is not None
    assert cfg.artifact_emit_roles() == ["author"]
    assert cfg.assess_roles() == ["round_assessor"]


def test_tool_call_hook_counts_without_blocking():
    registry = PluginRegistry()
    plugin_module.register(PluginAPI("sciteam-plugin-example", registry))
    plugin_module.reset_tool_call_count()

    result_a = registry.emit("tool_call", {"tool": "echo"})
    result_b = registry.emit("tool_call", {"tool": "bash"})

    assert result_a.block is False
    assert result_b.block is False
    assert plugin_module.tool_call_count == 2


def test_discovered_via_load_all_plugins_entry_point():
    """Only passes if this package was installed with its `sciteam.plugins`
    entry point registered (`pip install -e .` in this directory) — proves
    the packaging, not just the Python module."""
    registry = load_all_plugins(strict=True)
    ids = [m.id for m in registry.loaded_plugins]
    assert "sciteam-plugin-example" in ids
    assert registry.tool_handler("echo") is not None
