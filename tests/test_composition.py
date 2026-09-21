"""E6 (28-... §7.2): the single composition root."""

from __future__ import annotations

from pathlib import Path

import pytest
from sciteam.composition import CompositionError, CompositionRoot
from sciteam.plugin_api import PluginAPI, PluginManifest
from sciteam.resilience import ResilientRuntimePort


class _FakeRuntime:
    def version(self) -> str:
        return "fake-1.0"

    async def run_subagent(self, **kwargs):  # pragma: no cover - not exercised here
        raise NotImplementedError


def test_build_loads_plugins_and_creates_session_store(tmp_path: Path):
    root = CompositionRoot.build(workspace=tmp_path, run_id="run1")
    assert root.run_id == "run1"
    assert root.workspace == tmp_path
    assert (tmp_path).exists()
    assert root.session_store is not None


def test_resolve_runtime_wraps_in_resilience(tmp_path: Path, monkeypatch):
    root = CompositionRoot.build(workspace=tmp_path, run_id="run1")

    def fake_factory():
        return _FakeRuntime()

    root.plugins._register_runtime_port("fake", fake_factory)
    wrapped = root.resolve_runtime("fake")
    assert isinstance(wrapped, ResilientRuntimePort)
    assert wrapped.version() == "fake-1.0"


def test_resolve_runtime_unknown_key_raises_composition_error(tmp_path: Path):
    root = CompositionRoot.build(workspace=tmp_path, run_id="run1")
    with pytest.raises(CompositionError):
        root.resolve_runtime("does-not-exist")


def test_build_llm_worker_wires_session_store_and_plugin_registry(tmp_path: Path):
    from sciteam import PromptAssets

    from tests.conftest import DATA_ROOT

    root = CompositionRoot.build(workspace=tmp_path, run_id="run1")
    assets = PromptAssets(
        prompts_dir=DATA_ROOT / "assets" / "prompts", skills_dir=DATA_ROOT / "assets" / "skills"
    )

    class _Client:
        config = type("Cfg", (), {"default_max_tokens": 8192})()

    worker = root.build_llm_worker(
        client=_Client(), assets=assets, schemas_dir=DATA_ROOT / "schemas"
    )
    assert worker._session_store is root.session_store
    assert worker._plugin_registry is root.plugins


def test_guard_dispatches_to_plugin_hooks(tmp_path: Path):
    root = CompositionRoot.build(workspace=tmp_path, run_id="run1")
    api = PluginAPI("p", root.plugins)
    seen = []
    api.on("veto_issued", lambda payload: seen.append(payload) or None)
    root.plugins._register_manifest(
        PluginManifest(id="p", version="0.1.0", sciteam_api_version="1.0")
    )
    result = root.guard("veto_issued", {"subject": "m1"})
    assert not result.block
    assert seen == [{"subject": "m1"}]


def test_provenance_extra_lists_loaded_plugins(tmp_path: Path):
    root = CompositionRoot.build(workspace=tmp_path, run_id="run1")
    root.plugins._register_manifest(
        PluginManifest(id="demo", version="1.2.3", sciteam_api_version="1.0")
    )
    extra = root.provenance_extra()
    assert any("demo@1.2.3" in p for p in extra["plugins"])
    assert extra["session_store_root"] == str(tmp_path)
