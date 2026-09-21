"""Official Anthropic Claude RuntimePort plugin for SciTeam."""

from __future__ import annotations

from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginManifest

from sciteam_runtime_anthropic.runtime import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicRuntimePort,
)

__all__ = ["AnthropicRuntimePort", "DEFAULT_ANTHROPIC_MODEL", "register"]


def register(api: PluginAPI) -> PluginManifest:
    """Register the Anthropic runtime port under the 'anthropic' provider key."""
    api.register_runtime_port("anthropic", lambda: AnthropicRuntimePort())
    return PluginManifest(
        id="sciteam-runtime-anthropic",
        version="0.1.0",
        sciteam_api_version=PLUGIN_API_VERSION,
        provides=("runtime",),
        description="Official Anthropic Claude RuntimePort provider for SciTeam",
    )
