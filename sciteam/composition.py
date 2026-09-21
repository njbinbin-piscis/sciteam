"""The single composition root — 28-sciteam-productization-roadmap.md §7.2 (E6).

Before this module, `sciteam/canonical_demo.py`, `sciteam/cli.py` and
`harness/run_campaign.py` each assembled their own subset of: plugin
discovery, `ResilientRuntimePort` wrapping, the session store, and the tool
catalog/hook wiring `tool_loop.py`/`llm_worker.py` now support. That let one
entry point exercise a capability (say, resilience) that another silently
skipped — the exact "E1/E2 features validated only in `canonical_demo.py`"
gap flagged in the roadmap's re-evaluation.

``CompositionRoot`` is not a framework: it is one small object that does the
wiring every entry point needs, once, so "did you remember to wrap the
runtime in resilience / attach the session store / load plugins" stops being
a question answered differently by each caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sciteam.plugin_api import PluginRegistry, load_all_plugins
from sciteam.resilience import ResiliencePolicy, ResilientRuntimePort
from sciteam.session import FileSessionStore

if TYPE_CHECKING:
    from sciteam.runtime import RuntimePort

logger = logging.getLogger(__name__)


class CompositionError(RuntimeError):
    """Raised when the composition root cannot satisfy a request — e.g. an
    unknown runtime provider key. Never silently returns a degraded stand-in
    (no fail-open, same discipline the rest of this package follows)."""


@dataclass(frozen=True, slots=True)
class CompositionConfig:
    """R6-style single config path: default -> explicit constructor args ->
    env (via `ResiliencePolicy.from_env()`). Nothing here reads `SCITEAM_*`
    directly; that stays centralized in `resilience.py`/`limits.py`."""

    workspace: Path
    run_id: str
    strict_plugins: bool = False
    resilience_policy: ResiliencePolicy | None = None
    # docs/28 阶段4: `None` defers to `sandbox.resolve_sandbox_profile()`
    # (env `SCITEAM_SANDBOX_PROFILE` > product default `unattended`).
    sandbox_profile: str | None = None


class CompositionRoot:
    """One assembled set of run-wide collaborators: plugins, session store,
    resilience policy. Constructed once per run/process; every entry point
    (CLI, SDK, canonical demo, a campaign harness script) should build one
    of these instead of open-coding `load_all_plugins()` + a bare runtime.
    """

    def __init__(self, config: CompositionConfig) -> None:
        self._config = config
        self.plugins: PluginRegistry = load_all_plugins(strict=config.strict_plugins)
        self.session_store = FileSessionStore(config.workspace)
        self.resilience_policy = config.resilience_policy or ResiliencePolicy.from_env()

    @classmethod
    def build(
        cls,
        *,
        workspace: Path | str,
        run_id: str,
        strict_plugins: bool = False,
        resilience_policy: ResiliencePolicy | None = None,
        sandbox_profile: str | None = None,
    ) -> CompositionRoot:
        return cls(
            CompositionConfig(
                workspace=Path(workspace),
                run_id=run_id,
                strict_plugins=strict_plugins,
                resilience_policy=resilience_policy,
                sandbox_profile=sandbox_profile,
            )
        )

    @property
    def run_id(self) -> str:
        return self._config.run_id

    @property
    def workspace(self) -> Path:
        return self._config.workspace

    def resolve_runtime(self, name: str) -> RuntimePort:
        """Look up an installed runtime provider plugin by its registered
        key and return it wrapped in resilience — never the bare backend.
        A live run assembled through this method gets the same
        timeout/retry/circuit-breaker protection `tests/test_chaos.py`
        already proves for the harness-level campaign path, closing the gap
        where `canonical_demo.py --runtime` previously handed out an
        unwrapped provider.
        """
        factory = self.plugins.runtime_port_factory(name)
        if factory is None:
            available = sorted(self.plugins.loaded_plugins, key=lambda m: m.id)
            raise CompositionError(
                f"runtime provider {name!r} not found in registered plugins "
                f"(loaded: {[m.id for m in available]})"
            )
        return self.wrap_runtime(factory())

    def wrap_runtime(self, base: RuntimePort) -> ResilientRuntimePort:
        return ResilientRuntimePort(base, policy=self.resilience_policy)

    def build_llm_worker(self, **kwargs: Any):
        """Construct an `LlmWorkerRuntime` with this root's session store
        and plugin registry already injected — the one place `session_store`
        and `plugin_registry` are wired together, so a caller cannot attach
        one without the other by omission."""
        from sciteam.llm_worker import LlmWorkerRuntime

        kwargs.setdefault("session_store", self.session_store)
        kwargs.setdefault("plugin_registry", self.plugins)
        kwargs.setdefault("sandbox_profile", self._config.sandbox_profile)
        return LlmWorkerRuntime(**kwargs)

    def guard(self, event: str, payload: dict[str, Any]):
        """Direct access to the plugin hook dispatch, for composition-root
        callers (e.g. a mission runner's own artifact commit path) that are
        not going through `LlmWorkerRuntime`."""
        return self.plugins.emit(event, payload)

    def provenance_extra(self) -> dict[str, Any]:
        """Fields worth folding into `provenance.py`'s environment
        fingerprint: which plugins this run actually had loaded, and under
        which API version — a run's reproducibility claim is incomplete
        without naming its plugin set."""
        return {
            "plugins": sorted(
                f"{m.id}@{m.version} (api {m.sciteam_api_version})"
                for m in self.plugins.loaded_plugins
            ),
            "session_store_root": str(self.workspace),
        }
