"""Plugin engine contract (docs/25-sciteam-plugin-architecture.md).

Exercises `PluginRegistry`/`PluginAPI`/`load_all_plugins` directly against
fake `register()` callables — no real installed package needed, since
`load_all_plugins` only depends on `importlib.metadata.entry_points()`
returning objects with `.name` and `.load()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sciteam.plugin_api import (
    BLOCKABLE_EVENTS,
    EVENTS,
    PLUGIN_API_VERSION,
    HookResult,
    PluginAPI,
    PluginError,
    PluginManifest,
    PluginRegistry,
    api_version_compatible,
    load_all_plugins,
)
from sciteam.tool_catalog import ToolSpec


def _manifest(id_: str = "test-plugin", *, sciteam_api_version: str = PLUGIN_API_VERSION):
    return PluginManifest(id=id_, version="0.1.0", sciteam_api_version=sciteam_api_version)


class TestApiVersionCompatibility:
    def test_same_major_minor_is_compatible(self):
        assert api_version_compatible("1.0", current="1.0")

    def test_same_major_different_minor_is_compatible(self):
        assert api_version_compatible("1.0", current="1.7")

    def test_different_major_is_incompatible(self):
        assert not api_version_compatible("2.0", current="1.0")

    def test_empty_is_incompatible(self):
        assert not api_version_compatible("", current="1.0")


class TestPluginApiRegistration:
    """Direct PluginAPI -> PluginRegistry wiring, no discovery involved."""

    def test_register_tool_then_lookup(self):
        registry = PluginRegistry()
        api = PluginAPI("p1", registry)
        spec = ToolSpec(name="echo", version="1.0", capability="demo.echo", summary="Echoes input")
        api.register_tool(spec, handler=lambda args: {"echo": args})

        assert registry.tool_spec("echo") is spec
        handler = registry.tool_handler("echo")
        assert handler({"x": 1}) == {"echo": {"x": 1}}

    def test_register_runtime_port(self):
        registry = PluginRegistry()
        api = PluginAPI("p1", registry)
        sentinel = object()
        api.register_runtime_port("my_backend", lambda: sentinel)
        factory = registry.runtime_port_factory("my_backend")
        assert factory is not None
        assert factory() is sentinel

    def test_register_port_scoped_by_kind(self):
        registry = PluginRegistry()
        api = PluginAPI("p1", registry)
        api.register_port("compute", "local_docker", lambda: "compute-impl")
        api.register_port("literature", "local_docker", lambda: "lit-impl")

        assert registry.port_factory("compute", "local_docker")() == "compute-impl"
        assert registry.port_factory("literature", "local_docker")() == "lit-impl"
        assert registry.port_factory("dataset", "local_docker") is None

    def test_register_asset_dirs_accumulate(self, tmp_path):
        registry = PluginRegistry()
        api = PluginAPI("p1", registry)
        api.register_paradigm_dir(tmp_path / "paradigms")
        api.register_role_prompts_dir(tmp_path / "roles")
        api.register_skills_dir(tmp_path / "skills")

        assert registry.paradigm_dirs == (tmp_path / "paradigms",)
        assert registry.role_prompts_dirs == (tmp_path / "roles",)
        assert registry.skills_dirs == (tmp_path / "skills",)

    def test_duplicate_tool_name_within_one_registry_rejected(self):
        registry = PluginRegistry()
        api = PluginAPI("p1", registry)
        spec = ToolSpec(name="dup", version="1.0", capability="x", summary="")
        api.register_tool(spec, handler=lambda args: args)
        with pytest.raises(PluginError, match="already registered"):
            api.register_tool(spec, handler=lambda args: args)

    def test_on_rejects_unknown_event(self):
        registry = PluginRegistry()
        api = PluginAPI("p1", registry)
        with pytest.raises(PluginError, match="unknown plugin event"):
            api.on("mission_completed_and_also_lunch", lambda payload: None)


class TestHookDispatch:
    def test_observe_only_hook_never_blocks(self):
        registry = PluginRegistry()
        seen = []
        registry._register_hook("p1", "mission_start", lambda payload: seen.append(payload))
        result = registry.emit("mission_start", {"mission_id": "m1"})
        assert result == HookResult(block=False, reason="")
        assert seen == [{"mission_id": "m1"}]

    def test_tool_call_hook_can_block(self):
        registry = PluginRegistry()
        registry._register_hook(
            "guard-plugin",
            "tool_call",
            lambda payload: (
                HookResult(block=True, reason="denied by policy")
                if payload.get("tool") == "bash"
                else None
            ),
        )
        blocked = registry.emit("tool_call", {"tool": "bash"})
        assert blocked.block is True
        assert blocked.reason == "denied by policy"

        allowed = registry.emit("tool_call", {"tool": "read_file"})
        assert allowed.block is False

    def test_earlier_block_is_not_overridden_by_a_later_handler(self):
        registry = PluginRegistry()
        registry._register_hook(
            "strict-plugin",
            "artifact_submitted",
            lambda payload: HookResult(block=True, reason="no"),
        )
        registry._register_hook(
            "lenient-plugin",
            "artifact_submitted",
            lambda payload: HookResult(block=False, reason=""),
        )
        result = registry.emit("artifact_submitted", {})
        assert result.block is True
        assert result.reason == "no"

    def test_a_raising_hook_does_not_break_the_mission(self):
        registry = PluginRegistry()

        def _bad_hook(payload: dict[str, Any]) -> HookResult | None:
            raise RuntimeError("boom")

        registry._register_hook("broken-plugin", "round_end", _bad_hook)
        # Must not raise.
        result = registry.emit("round_end", {})
        assert result.block is False

    def test_only_tool_call_and_artifact_submitted_are_blockable_by_design(self):
        assert {"tool_call", "artifact_submitted"} == BLOCKABLE_EVENTS
        assert BLOCKABLE_EVENTS <= EVENTS


@dataclass
class _FakeEntryPoint:
    name: str
    _register: Any

    def load(self):
        return self._register


class TestLoadAllPlugins:
    def test_loads_a_well_behaved_plugin(self, monkeypatch):
        def register(api: PluginAPI) -> PluginManifest:
            api.register_tool(
                ToolSpec(name="hello", version="1.0", capability="demo", summary="hi"),
                handler=lambda args: {"ok": True},
            )
            return _manifest("good-plugin")

        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: [_FakeEntryPoint("good", register)],
        )
        registry = load_all_plugins(strict=True)
        assert [m.id for m in registry.loaded_plugins] == ["good-plugin"]
        assert registry.tool_handler("hello") is not None

    def test_incompatible_api_version_is_skipped_not_fatal(self, monkeypatch, caplog):
        def register(api: PluginAPI) -> PluginManifest:
            api.register_tool(
                ToolSpec(name="future_tool", version="1.0", capability="x", summary=""),
                handler=lambda args: args,
            )
            return _manifest("future-plugin", sciteam_api_version="99.0")

        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: [_FakeEntryPoint("future", register)],
        )
        registry = load_all_plugins(strict=False)
        assert registry.loaded_plugins == ()
        # Fail-closed: nothing that plugin tried to register leaked through.
        assert registry.tool_handler("future_tool") is None

    def test_incompatible_api_version_raises_in_strict_mode(self, monkeypatch):
        def register(api: PluginAPI) -> PluginManifest:
            return _manifest("future-plugin", sciteam_api_version="99.0")

        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: [_FakeEntryPoint("future", register)],
        )
        with pytest.raises(PluginError, match="incompatible"):
            load_all_plugins(strict=True)

    def test_duplicate_plugin_id_across_two_entry_points_second_one_skipped(self, monkeypatch):
        def register_a(api: PluginAPI) -> PluginManifest:
            api.register_tool(
                ToolSpec(name="a_tool", version="1.0", capability="x", summary=""),
                handler=lambda args: args,
            )
            return _manifest("same-id")

        def register_b(api: PluginAPI) -> PluginManifest:
            api.register_tool(
                ToolSpec(name="b_tool", version="1.0", capability="x", summary=""),
                handler=lambda args: args,
            )
            return _manifest("same-id")

        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: [
                _FakeEntryPoint("a", register_a),
                _FakeEntryPoint("b", register_b),
            ],
        )
        registry = load_all_plugins(strict=False)
        assert [m.id for m in registry.loaded_plugins] == ["same-id"]
        # First one wins; second's tool never leaked into the final registry.
        assert registry.tool_handler("a_tool") is not None
        assert registry.tool_handler("b_tool") is None

    def test_one_broken_plugin_does_not_prevent_others_from_loading(self, monkeypatch):
        def register_broken(api: PluginAPI) -> PluginManifest:
            raise RuntimeError("this plugin is broken")

        def register_good(api: PluginAPI) -> PluginManifest:
            return _manifest("good-plugin")

        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: [
                _FakeEntryPoint("broken", register_broken),
                _FakeEntryPoint("good", register_good),
            ],
        )
        registry = load_all_plugins(strict=False)
        assert [m.id for m in registry.loaded_plugins] == ["good-plugin"]

    def test_register_returning_wrong_type_is_rejected(self, monkeypatch):
        def register(api: PluginAPI) -> str:
            return "not a manifest"  # type: ignore[return-value]

        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: [_FakeEntryPoint("bad", register)],
        )
        registry = load_all_plugins(strict=False)
        assert registry.loaded_plugins == ()

    def test_import_sciteam_never_loads_plugins_implicitly(self, monkeypatch):
        """Loading is an explicit composition-root step (see module
        docstring) — this test pins that `import sciteam` on its own never
        calls `importlib.metadata.entry_points` for the plugins group."""
        calls: list[Any] = []
        monkeypatch.setattr(
            "sciteam.plugin_api.metadata.entry_points",
            lambda group=None: calls.append(group) or [],
        )
        import importlib

        import sciteam

        importlib.reload(sciteam)
        assert calls == []
