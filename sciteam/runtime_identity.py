"""Pinnable version strings for whatever is running behind a seat.

A run's ledger is only as reproducible as its ability to say *which build*
produced it. This module answers that for the two cases that exist: a
backend that knows its own version (ask it), and the local checkout or
image running the bundled runtime (derive it).

The answer is never a bare label like ``"local"`` — an unpinnable version
string is recorded as ``unknown`` so it is visibly missing in the ledger
rather than quietly indistinguishable from a real one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

#: The source checkout `git describe` runs against. Meaningless for an
#: installed wheel — hence the SCITEAM_IMAGE_DIGEST path below, which is how
#: a containerized or installed deployment pins itself.
CHECKOUT_ROOT = Path(__file__).resolve().parents[1]

IMAGE_DIGEST_ENV = "SCITEAM_IMAGE_DIGEST"


def describe_local_runtime(checkout_root: Path | None = None) -> str:
    """Version of the bundled runtime: image digest if declared, else the
    checkout's `git describe`, else ``"local unknown"``."""
    digest = os.environ.get(IMAGE_DIGEST_ENV, "").strip()
    if digest:
        return f"local image:{digest}"
    root = Path(checkout_root) if checkout_root is not None else CHECKOUT_ROOT
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=12"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "local unknown"
    desc = (proc.stdout or "").strip()
    if proc.returncode == 0 and desc:
        return f"local {desc}"
    return "local unknown"


def runtime_version(runtime: Any) -> str:
    """Version of the backend behind a seat.

    A `RuntimePort` implementation that exposes a callable `version()` —
    typically a plugin wrapping an external agent CLI, which knows its own
    build — reports it. Anything else falls back to the local runtime's
    version, since the bundled worker *is* this package.
    """
    version = getattr(runtime, "version", None)
    if callable(version):
        try:
            reported = str(version()).strip()
        except Exception:  # noqa: BLE001 - a backend must not break provenance recording
            return "unknown"
        if reported:
            return reported
    return describe_local_runtime()
