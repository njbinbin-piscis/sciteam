"""What produced this run, and what could not be pinned down.

Two runs that both say "success" are not evidence of anything if nobody
recorded that they used different builds of the same named dependency. So
every run records a fingerprint of its environment — and, just as
importantly, an explicit list of the things it *could not* pin.

That second list is the point. A fingerprint that quietly omits the
unpinnable parts manufactures a false sense of reproducibility, which is
worse than no fingerprint at all: the largest uncontrolled variable in an
LLM run is usually a hosted model endpoint whose weights the vendor can
change without notice or version bump. :func:`environment_fingerprint`
therefore always reports ``unpinnable`` alongside ``pinned``, and
:func:`fingerprint_digest` only ever digests the pinned half — so two runs
agreeing on the digest means "same controlled environment", never "same
result guaranteed".

Nothing here reads a secret's value. Environment variables whose names look
like credentials are recorded as present-and-redacted, because *whether* a
key was configured changes a run's behaviour while its value must never
reach an artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from importlib import metadata
from typing import Any

from sciteam._paths import DATA_ROOT
from sciteam.runtime_identity import IMAGE_DIGEST_ENV, describe_local_runtime

#: Env var name fragments that mark a value as a credential. Presence is
#: recorded (it changes behaviour); the value never is.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

#: Distributions worth pinning: our own declared runtime dependencies. The
#: full `pip freeze` is deliberately *not* captured — it is mostly noise
#: that changes for unrelated reasons, which makes digests useless for
#: telling meaningful environment drift from incidental drift.
_TRACKED_DISTRIBUTIONS = ("sciteam", "pyyaml", "jsonschema")

REDACTED = "<set:redacted>"

#: Variables that only say *where output goes*. They are recorded, but kept
#: out of the digest: two runs that differ solely in their output directory
#: are the same experiment, and flagging that as environment drift trains
#: people to ignore the check. Anything that changes what gets *loaded*
#: (notably ``SCITEAM_ASSETS_ROOT``, a different institution) stays pinned.
_OUTPUT_ONLY_ENV = frozenset({"SCITEAM_RUNS_ROOT", "SCITEAM_PACK_DIR"})


def _is_secret(name: str) -> bool:
    return any(marker in name.upper() for marker in _SECRET_MARKERS)


def sciteam_env_overrides(*, output_only: bool = False) -> dict[str, str]:
    """``SCITEAM_*`` variables in effect, with credentials redacted.

    Inherited host state is exactly the kind of covariate that makes a
    result unreproducible on someone else's machine, so the run records
    which knobs were turned rather than assuming defaults.
    """
    return {
        name: REDACTED if _is_secret(name) else value
        for name, value in sorted(os.environ.items())
        if name.startswith("SCITEAM_") and (name in _OUTPUT_ONLY_ENV) == output_only
    }


def tracked_dependencies() -> dict[str, str]:
    """Installed versions of the distributions this package depends on."""
    versions: dict[str, str] = {}
    for name in _TRACKED_DISTRIBUTIONS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def installed_plugins() -> list[str]:
    """``id@version`` for each discoverable plugin distribution.

    Read from entry-point metadata rather than by importing anything: this
    must stay side-effect free, since it runs while recording provenance.
    """
    from sciteam.plugin_api import ENTRY_POINT_GROUP

    found: list[str] = []
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = getattr(ep, "dist", None)
        version = getattr(dist, "version", None) or "unknown"
        found.append(f"{ep.name}@{version}")
    return sorted(found)


def _pinned() -> dict[str, Any]:
    # Imported here, not at module scope: `sciteam/__init__` re-exports this
    # module, so a top-level import would be circular.
    from sciteam import __version__

    return {
        "sciteam_version": __version__,
        "sciteam_build": describe_local_runtime(),
        "image_digest": os.environ.get(IMAGE_DIGEST_ENV, "").strip() or None,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "dependencies": tracked_dependencies(),
        "plugins": installed_plugins(),
        "env_overrides": sciteam_env_overrides(),
    }


def _context() -> dict[str, Any]:
    """Recorded for debugging, deliberately *outside* the digest.

    Install paths differ between two otherwise identical containers. Folding
    them into the digest would make every comparison report drift that means
    nothing, which is the fastest way to teach people to ignore the check.
    """
    return {
        "python_executable": sys.executable,
        "data_root": str(DATA_ROOT),
        "output_env": sciteam_env_overrides(output_only=True),
    }


def _unpinnable(pinned: dict[str, Any], models: list[str] | None) -> list[dict[str, str]]:
    """Name every uncontrolled variable, with the reason it is uncontrolled."""
    gaps: list[dict[str, str]] = []
    for model in models or []:
        gaps.append(
            {
                "what": f"model:{model}",
                "why": (
                    "hosted third-party endpoint: the provider can change weights, "
                    "routing or defaults without a version change, so an identical "
                    "model id does not guarantee an identical model"
                ),
            }
        )
    if not pinned["image_digest"]:
        build = pinned["sciteam_build"]
        gaps.append(
            {
                "what": "runtime image",
                "why": (
                    f"no {IMAGE_DIGEST_ENV} declared; provenance falls back to "
                    f"{build!r}, which is reproducible only for someone with this "
                    "exact checkout"
                    if build != "local unknown"
                    else f"no {IMAGE_DIGEST_ENV} declared and the build could not be "
                    "derived from a checkout — this run is not reproducible"
                ),
            }
        )
    missing = [name for name, v in pinned["dependencies"].items() if v == "not-installed"]
    if missing:
        gaps.append(
            {
                "what": "dependencies:" + ",".join(missing),
                "why": "declared dependency not found in this environment",
            }
        )
    return gaps


def environment_fingerprint(*, models: list[str] | None = None) -> dict[str, Any]:
    """Full provenance record: what is pinned, what is not, and a digest.

    ``models`` are the model identifiers this run intends to use; each is
    reported as unpinnable, since a hosted endpoint is outside our control.
    Pass none for a fully scripted run — which is precisely why the
    canonical demo is reproducible and a live run is not.
    """
    pinned = _pinned()
    unpinnable = _unpinnable(pinned, models)
    return {
        "pinned": pinned,
        "pinned_digest": fingerprint_digest(pinned),
        "context": _context(),
        "unpinnable": unpinnable,
        "reproducible": not unpinnable,
    }


def fingerprint_digest(pinned: dict[str, Any]) -> str:
    """sha256 over the pinned half only — see this module's docstring."""
    canonical = json.dumps(pinned, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compare(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Human-readable differences between two fingerprints' pinned halves.

    Used when a replayed or resumed run does not match its baseline: the
    useful output is *which* covariate moved, not that a digest differs.
    """
    diffs: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key), b.get(key), f"{path}.{key}" if path else key)
        elif a != b:
            diffs.append(f"{path}: {a!r} -> {b!r}")

    walk(left.get("pinned", left), right.get("pinned", right), "")
    return diffs
