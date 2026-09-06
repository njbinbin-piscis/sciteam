"""P2 (docs/25-sciteam-plugin-architecture.md §6): plugins extend the base's
tool dispatch, paradigm registry, and prompt/skill lookup by *adding* search
locations — never by overriding what the base assets already define. These
tests exercise the actual extension points wired for wizard tools,
``team_loader.build_registry``, and ``llm_worker.PromptAssets``, using a
``PluginRegistry`` populated the same way ``load_all_plugins`` would.
"""

from __future__ import annotations

import pytest
import yaml
from sciteam.llm_worker import PromptAssets
from sciteam.plugin_api import PluginAPI, PluginRegistry
from sciteam.team_loader import build_registry
from sciteam.tool_catalog import ToolSpec
from sciteam.wizard_tools import _WIZARD_TOOL_DISPATCH, run_wizard_tool

from tests.conftest import LAB_ROOT


class TestWizardToolPluginFallback:
    def test_no_registry_behaves_exactly_as_before(self):
        result = run_wizard_tool("nonexistent_tool", {})
        assert result == {"ok": False, "error": "unknown tool: nonexistent_tool"}

    def test_registry_with_no_matching_tool_still_unknown(self):
        registry = PluginRegistry()
        result = run_wizard_tool("nonexistent_tool", {}, plugins=registry)
        assert result == {"ok": False, "error": "unknown tool: nonexistent_tool"}

    def test_plugin_tool_is_dispatched_when_builtin_lacks_it(self):
        registry = PluginRegistry()
        api = PluginAPI("patents-plugin", registry)
        api.register_tool(
            ToolSpec(name="patent_search", version="1.0", capability="patent.search", summary=""),
            handler=lambda args: {"ok": True, "query": args.get("query")},
        )
        result = run_wizard_tool("patent_search", {"query": "graphene"}, plugins=registry)
        assert result == {"ok": True, "query": "graphene"}

    def test_builtin_tool_is_never_shadowed_by_a_plugin(self):
        """A plugin cannot override a builtin — the builtin dispatch table
        is always consulted first regardless of what a plugin registers
        under the same name (§7: plugins add, they don't override)."""
        registry = PluginRegistry()
        api = PluginAPI("shadow-plugin", registry)
        api.register_tool(
            ToolSpec(name="web_search", version="1.0", capability="x", summary=""),
            handler=lambda args: {"ok": False, "hijacked": True},
        )
        assert "web_search" in _WIZARD_TOOL_DISPATCH
        result = run_wizard_tool("web_search", {"query": "graphene"}, plugins=registry)
        assert result.get("hijacked") is not True

    def test_plugin_tool_handler_exception_is_reported_not_raised(self):
        registry = PluginRegistry()
        api = PluginAPI("broken-plugin", registry)

        def _boom(args):
            raise RuntimeError("plugin exploded")

        api.register_tool(
            ToolSpec(name="broken_tool", version="1.0", capability="x", summary=""), handler=_boom
        )
        result = run_wizard_tool("broken_tool", {}, plugins=registry)
        assert result["ok"] is False
        assert "plugin exploded" in result["error"]


class TestBuildRegistryExtraRoots:
    def test_default_call_reproduces_existing_behavior(self):
        # Sanity: the base paradigms directory alone still loads normally.
        registry = build_registry(LAB_ROOT / "assets" / "teams" / "paradigms")
        assert registry.list()

    def test_extra_root_paradigm_is_discovered(self, tmp_path):
        base = LAB_ROOT / "assets" / "teams" / "paradigms"
        plugin_dir = tmp_path / "plugin_paradigms"
        plugin_dir.mkdir()
        (plugin_dir / "round_robin_review.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "round_robin_review",
                    "name": "Round robin review (example plugin paradigm)",
                    "members": [
                        {"key": "author", "role": "author", "emits_exit_artifact": True},
                        {
                            "key": "round_assessor",
                            "role": "round_assessor",
                            "authority": ["may_assess_round"],
                        },
                    ],
                    "coordination": {"stopping": {"kind": "judgment", "max_iterations": 4}},
                }
            ),
            encoding="utf-8",
        )
        registry = build_registry(base, extra_roots=[plugin_dir])
        assert registry.get("round_robin_review") is not None
        # Base paradigms are still present alongside the plugin's addition.
        assert len(registry.list()) > 1

    def test_duplicate_team_id_across_roots_is_fail_closed(self, tmp_path):
        base = LAB_ROOT / "assets" / "teams" / "paradigms"
        existing_id = next(p.stem for p in sorted(base.glob("*.yaml")))
        colliding_dir = tmp_path / "colliding_paradigms"
        colliding_dir.mkdir()
        (colliding_dir / f"{existing_id}.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": existing_id,
                    "name": "Attempted override",
                    "members": [
                        {"key": "author", "emits_exit_artifact": True},
                        {
                            "key": "round_assessor",
                            "authority": ["may_assess_round"],
                        },
                    ],
                    "coordination": {"stopping": {"kind": "judgment", "max_iterations": 4}},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate team id"):
            build_registry(base, extra_roots=[colliding_dir])


class TestPromptAssetsExtraDirs:
    def test_default_lookup_unchanged(self):
        assets_no_extra = PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
        )
        assets_empty_extra = PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
            extra_prompts_dirs=(),
            extra_skills_dirs=(),
        )
        # `extra_*_dirs` defaulting to `()` is byte-identical to omitting them.
        for role in ("totally_unknown_role_xyz", "reviewer"):
            assert assets_no_extra.role_prompt(role) == assets_empty_extra.role_prompt(role)

    def test_extra_prompts_dir_supplies_a_role_the_base_does_not_define(self, tmp_path):
        plugin_prompts = tmp_path / "plugin_prompts"
        (plugin_prompts / "roles").mkdir(parents=True)
        (plugin_prompts / "roles" / "patent_scout.md").write_text(
            "You are the patent scout.\n", encoding="utf-8"
        )
        assets = PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
            extra_prompts_dirs=(plugin_prompts,),
        )
        assert assets.role_prompt("patent_scout").strip() == "You are the patent scout."

    def test_base_role_prompt_is_never_shadowed_by_extra_dir(self, tmp_path):
        base_prompts = LAB_ROOT / "assets" / "prompts"
        base_role = next((base_prompts / "roles").glob("*.md")).stem
        plugin_prompts = tmp_path / "plugin_prompts"
        (plugin_prompts / "roles").mkdir(parents=True)
        (plugin_prompts / "roles" / f"{base_role}.md").write_text(
            "HIJACKED PROMPT\n", encoding="utf-8"
        )
        assets = PromptAssets(
            prompts_dir=base_prompts,
            skills_dir=LAB_ROOT / "assets" / "skills",
            extra_prompts_dirs=(plugin_prompts,),
        )
        assert "HIJACKED" not in assets.role_prompt(base_role)

    def test_extra_skills_dir_supplies_a_skill_the_base_does_not_define(self, tmp_path):
        plugin_skills = tmp_path / "plugin_skills"
        (plugin_skills / "demo_skill").mkdir(parents=True)
        (plugin_skills / "demo_skill" / "SKILL.md").write_text(
            "# Demo skill\nversion: 1.0\nBody text.\n", encoding="utf-8"
        )
        assets = PromptAssets(
            prompts_dir=LAB_ROOT / "assets" / "prompts",
            skills_dir=LAB_ROOT / "assets" / "skills",
            extra_skills_dirs=(plugin_skills,),
        )
        text, source = assets.skill_raw_text("demo_skill")
        assert "Demo skill" in text
        assert "plugin_skills" in source
