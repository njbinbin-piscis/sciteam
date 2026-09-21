"""Official DeepSeek RuntimePort plugin for SciTeam."""

from __future__ import annotations

from sciteam.plugin_api import PLUGIN_API_VERSION, PluginAPI, PluginManifest

from sciteam_runtime_deepseek.runtime import (
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekRuntimePort,
)

__all__ = ["DEFAULT_DEEPSEEK_MODEL", "DeepSeekRuntimePort", "register"]


def register(api: PluginAPI) -> PluginManifest:
    """Register the DeepSeek runtime port under the 'deepseek' provider key."""
    api.register_runtime_port("deepseek", lambda: DeepSeekRuntimePort())
    return PluginManifest(
        id="sciteam-runtime-deepseek",
        version="0.1.0",
        sciteam_api_version=PLUGIN_API_VERSION,
        provides=("runtime",),
        description="Official DeepSeek RuntimePort provider for SciTeam",
    )
