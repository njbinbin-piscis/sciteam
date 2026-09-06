"""The plugin engine bundled with the sciteam base (design: see
``docs/25-sciteam-plugin-architecture.md`` in the companion research repo).
Analogous to ``pluggy`` shipping inside ``pytest`` itself: this module is
part of the frozen base, not a plugin — it is what lets everything *else*
be a plugin instead of a change to ``sciteam/``.

Discovery is standard Python packaging (``importlib.metadata`` entry
points under the ``sciteam.plugins`` group) — no directory scanning, no
hot reload, no bespoke config file. A plugin is installed with
``pip install`` and found automatically; nothing about a plugin's own
code lives inside this package.

Loading is never an import side effect. ``import sciteam`` never touches
this module's discovery path — a composition root (a harness script, a
test fixture) calls :func:`load_all_plugins` explicitly. This mirrors
ADR-004's "no implicit global state": a unit test that never asked for
plugins must not have its behavior change because some unrelated package
happens to be installed in the same environment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sciteam.tool_catalog import ToolSpec

if TYPE_CHECKING:
    from sciteam.adaptive_planner import PlanReviser
    from sciteam.runtime import RuntimePort

logger = logging.getLogger(__name__)

#: Frozen plugin-facing contract version — independent of `sciteam.__version__`.
#: Internal refactors that do not touch the surface documented in
#: `docs/25-sciteam-plugin-architecture.md` §5 never bump this. Bumping the
#: major component is a breaking change to every installed plugin.
PLUGIN_API_VERSION = "1.0"

#: The `importlib.metadata` entry-point group every plugin package registers
#: `"my_plugin_module:register"` under, in its own `pyproject.toml`:
#:     [project.entry-points."sciteam.plugins"]
#:     my_plugin = "my_plugin:register"
ENTRY_POINT_GROUP = "sciteam.plugins"

PortKind = Literal["compute", "dataset", "literature", "verifier"]
ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]

#: A plugin's `on(event, handler)` hook. Returning `None` means "observe
#: only"; returning a `HookResult(block=True, reason=...)` vetoes the
#: action for hooks that are declared blockable (see `BLOCKABLE_EVENTS`).
HookHandler = Callable[[dict[str, Any]], "HookResult | None"]

#: Events a hook may subscribe to (`PluginAPI.on`). Kept in sync with
#: docs/25-sciteam-plugin-architecture.md §3.4 — that table is the source
#: of truth for what each event's payload contains and when it fires.
EVENTS = frozenset(
    {
        "mission_start",
        "round_end",
        "tool_call",
        "artifact_submitted",
        "veto_issued",
        "amendment_applied",
    }
)

#: Only these two events may block the action they observe. The rest of
#: `EVENTS` already have their own authority判定 elsewhere (declarative
#: roles in team_loader, the amendment layer whitelist, audit_veto's RBAC)
#: — a plugin gets to add *more* scrutiny at the two points where "does
#: this action take effect" is actually decided, never a second opinion
#: on a decision some other mechanism already made.
BLOCKABLE_EVENTS = frozenset({"tool_call", "artifact_submitted"})


class PluginError(RuntimeError):
    """Raised for plugin load/registration failures.

    In `load_all_plugins(strict=False)` (the default), a `PluginError` for
    one plugin is caught, logged, and that plugin's contribution is
    discarded — every other plugin still loads. `strict=True` re-raises
    immediately (CI / pre-release verification)."""


@dataclass(frozen=True)
class PluginManifest:
    id: str
    version: str
    sciteam_api_version: str
    provides: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class HookResult:
    block: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _ToolRegistration:
    spec: ToolSpec
    handler: ToolHandler
    plugin_id: str


def api_version_compatible(requested: str, *, current: str = PLUGIN_API_VERSION) -> bool:
    """Same major component => compatible (`MAJOR.MINOR`, semver-ish)."""
    requested_major = (requested or "").split(".", 1)[0]
    current_major = current.split(".", 1)[0]
    return bool(requested_major) and requested_major == current_major


class PluginRegistry:
    """Aggregates everything plugins register.

    One instance per process (or per test) — never a module-level
    singleton, so tests never leak plugin state into each other. Also
    used, unregistered, as each plugin's private staging area during
    loading (see `load_all_plugins`) so a plugin whose manifest fails
    validation never partially pollutes the final registry.
    """

    def __init__(self) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self._runtime_ports: dict[str, Callable[[], RuntimePort]] = {}
        self._tools: dict[str, _ToolRegistration] = {}
        self._ports: dict[tuple[PortKind, str], Callable[[], Any]] = {}
        self._plan_revisers: dict[str, PlanReviser] = {}
        self._assessors: dict[str, Callable[[], Any]] = {}
        self._paradigm_dirs: list[Path] = []
        self._role_prompts_dirs: list[Path] = []
        self._skills_dirs: list[Path] = []
        self._hooks: dict[str, list[tuple[str, HookHandler]]] = {}

    # -- lookups consumed by the rest of sciteam -------------------------
    def runtime_port_factory(self, name: str) -> Callable[[], RuntimePort] | None:
        return self._runtime_ports.get(name)

    def tool_handler(self, name: str) -> ToolHandler | None:
        reg = self._tools.get(name)
        return reg.handler if reg else None

    def tool_spec(self, name: str) -> ToolSpec | None:
        reg = self._tools.get(name)
        return reg.spec if reg else None

    def all_tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(reg.spec for reg in self._tools.values())

    def port_factory(self, kind: PortKind, name: str) -> Callable[[], Any] | None:
        return self._ports.get((kind, name))

    def plan_reviser(self, name: str) -> PlanReviser | None:
        return self._plan_revisers.get(name)

    def assessor_factory(self, name: str) -> Callable[[], Any] | None:
        return self._assessors.get(name)

    @property
    def paradigm_dirs(self) -> tuple[Path, ...]:
        return tuple(self._paradigm_dirs)

    @property
    def role_prompts_dirs(self) -> tuple[Path, ...]:
        return tuple(self._role_prompts_dirs)

    @property
    def skills_dirs(self) -> tuple[Path, ...]:
        return tuple(self._skills_dirs)

    @property
    def loaded_plugins(self) -> tuple[PluginManifest, ...]:
        return tuple(self._manifests.values())

    def emit(self, event: str, payload: dict[str, Any]) -> HookResult:
        """Dispatch `event` to every registered handler, in registration
        order. Every handler runs (so every plugin gets to observe/record
        regardless of an earlier block); the first `block=True` a handler
        returns wins — a later handler cannot un-block what an earlier one
        vetoed. A handler that raises is logged and treated as "observe
        only, no opinion" — one broken plugin hook must not take down a
        mission, matching sciteam's existing per-seat error isolation."""
        result = HookResult()
        for plugin_id, handler in self._hooks.get(event, ()):
            try:
                outcome = handler(payload)
            except Exception:  # noqa: BLE001 - a broken hook must not break the mission
                logger.exception("sciteam plugin %r: hook %r raised", plugin_id, event)
                continue
            if outcome is not None and outcome.block and not result.block:
                result = outcome
        return result

    # -- registration: only ever called by a PluginAPI bound to this
    #    registry (or by _merge_from, once a plugin's manifest is valid) --
    def _register_manifest(self, manifest: PluginManifest) -> None:
        self._manifests[manifest.id] = manifest

    def _register_runtime_port(self, name: str, factory: Callable[[], RuntimePort]) -> None:
        if name in self._runtime_ports:
            raise PluginError(f"runtime port {name!r} already registered")
        self._runtime_ports[name] = factory

    def _register_tool(self, plugin_id: str, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise PluginError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = _ToolRegistration(spec=spec, handler=handler, plugin_id=plugin_id)

    def _register_port(self, kind: PortKind, name: str, factory: Callable[[], Any]) -> None:
        key = (kind, name)
        if key in self._ports:
            raise PluginError(f"{kind} port {name!r} already registered")
        self._ports[key] = factory

    def _register_plan_reviser(self, name: str, reviser: PlanReviser) -> None:
        if name in self._plan_revisers:
            raise PluginError(f"plan reviser {name!r} already registered")
        self._plan_revisers[name] = reviser

    def _register_assessor(self, name: str, factory: Callable[[], Any]) -> None:
        if name in self._assessors:
            raise PluginError(f"assessor {name!r} already registered")
        self._assessors[name] = factory

    def _register_hook(self, plugin_id: str, event: str, handler: HookHandler) -> None:
        if event not in EVENTS:
            raise PluginError(f"unknown plugin event: {event!r} (known: {sorted(EVENTS)})")
        self._hooks.setdefault(event, []).append((plugin_id, handler))

    def _merge_from(self, staging: PluginRegistry, plugin_id: str) -> None:
        """Commit one plugin's staged registrations into this (final)
        registry, conflict-checked against what is already committed.
        Called only after that plugin's manifest has been validated —
        see `load_all_plugins`. Raises `PluginError` and commits nothing
        for this plugin if any single item conflicts (all-or-nothing per
        plugin, so a plugin never ends up half-registered)."""
        for name, factory in staging._runtime_ports.items():
            self._register_runtime_port(name, factory)
        for reg in staging._tools.values():
            self._register_tool(plugin_id, reg.spec, reg.handler)
        for (kind, name), factory in staging._ports.items():
            self._register_port(kind, name, factory)
        for name, reviser in staging._plan_revisers.items():
            self._register_plan_reviser(name, reviser)
        for name, factory in staging._assessors.items():
            self._register_assessor(name, factory)
        for event, handlers in staging._hooks.items():
            for owner, handler in handlers:
                self._register_hook(owner, event, handler)
        self._paradigm_dirs.extend(staging._paradigm_dirs)
        self._role_prompts_dirs.extend(staging._role_prompts_dirs)
        self._skills_dirs.extend(staging._skills_dirs)


class PluginAPI:
    """Bound to one plugin id and one (staging) `PluginRegistry`; passed
    to that plugin's `register(api) -> PluginManifest` entry point. Thin
    forwarding layer — all state lives on the registry."""

    def __init__(self, plugin_id: str, registry: PluginRegistry) -> None:
        self._plugin_id = plugin_id
        self._registry = registry

    def register_runtime_port(self, name: str, factory: Callable[[], RuntimePort]) -> None:
        self._registry._register_runtime_port(name, factory)

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._registry._register_tool(self._plugin_id, spec, handler)

    def register_port(self, kind: PortKind, name: str, factory: Callable[[], Any]) -> None:
        self._registry._register_port(kind, name, factory)

    def register_plan_reviser(self, name: str, reviser: PlanReviser) -> None:
        self._registry._register_plan_reviser(name, reviser)

    def register_assessor(self, name: str, factory: Callable[[], Any]) -> None:
        self._registry._register_assessor(name, factory)

    def register_paradigm_dir(self, path: Path | str) -> None:
        self._registry._paradigm_dirs.append(Path(path))

    def register_role_prompts_dir(self, path: Path | str) -> None:
        self._registry._role_prompts_dirs.append(Path(path))

    def register_skills_dir(self, path: Path | str) -> None:
        self._registry._skills_dirs.append(Path(path))

    def on(self, event: str, handler: HookHandler) -> None:
        self._registry._register_hook(self._plugin_id, event, handler)


def load_all_plugins(*, strict: bool = False) -> PluginRegistry:
    """Discover every package registered under the `sciteam.plugins`
    entry-point group, call its `register(api) -> PluginManifest` entry
    point, and aggregate the result into one `PluginRegistry`.

    Each plugin's registrations are staged in a private `PluginRegistry`
    first; they are only merged into the returned registry after that
    plugin's manifest passes id-uniqueness and API-version checks — a
    plugin that fails validation never leaves partial state behind.

    Never called implicitly by `import sciteam` — see module docstring.
    """
    registry = PluginRegistry()
    seen_ids: set[str] = set()
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        _load_one(entry_point, registry, seen_ids, strict=strict)
    return registry


def _load_one(
    entry_point: metadata.EntryPoint,
    registry: PluginRegistry,
    seen_ids: set[str],
    *,
    strict: bool,
) -> None:
    try:
        register_fn = entry_point.load()
    except Exception as exc:  # noqa: BLE001 - a bad plugin package must not break discovery
        _fail_or_warn(strict, f"failed to load plugin entry point {entry_point.name!r}: {exc}")
        return

    staging = PluginRegistry()
    try:
        manifest = register_fn(PluginAPI(entry_point.name, staging))
        if not isinstance(manifest, PluginManifest):
            raise PluginError(
                f"plugin {entry_point.name!r} register() must return a PluginManifest, "
                f"got {type(manifest).__name__}"
            )
        if manifest.id in seen_ids:
            raise PluginError(f"duplicate plugin id: {manifest.id!r}")
        if not api_version_compatible(manifest.sciteam_api_version):
            raise PluginError(
                f"plugin {manifest.id!r} targets sciteam plugin API "
                f"{manifest.sciteam_api_version!r}, incompatible with this "
                f"sciteam's {PLUGIN_API_VERSION!r}"
            )
        registry._merge_from(staging, manifest.id)
        registry._register_manifest(manifest)
        seen_ids.add(manifest.id)
    except PluginError as exc:
        _fail_or_warn(strict, str(exc))
    except Exception as exc:  # noqa: BLE001 - a broken register() must not break other plugins
        _fail_or_warn(strict, f"plugin {entry_point.name!r} register() raised: {exc}")


def _fail_or_warn(strict: bool, message: str) -> None:
    if strict:
        raise PluginError(message)
    logger.warning("sciteam plugin: %s", message)
