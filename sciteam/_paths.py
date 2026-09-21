"""Where SciTeam's bundled data lives — the single source of truth.

Institution manifests, role prompts, skills, JSON Schemas and contracts are
*data* the engine loads at runtime, and they ship **inside** the wheel under
``sciteam/data/`` so that a plain ``pip install sciteam`` (not just an
editable checkout) is self-sufficient. Every module that needs one of these
directories resolves it through this module; nothing else in ``sciteam/`` may
assume a repository layout such as ``Path(__file__).parents[1] / "assets"``.

Run output (``runs/``) is deliberately *not* bundled data: it is written
relative to the current working directory (or ``SCITEAM_RUNS_ROOT``), never
into the installed package.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PACKAGE_ROOT / "data"

ASSETS_ROOT = DATA_ROOT / "assets"
SCHEMAS_DIR = DATA_ROOT / "schemas"
CONTRACTS_DIR = DATA_ROOT / "contracts"

RUNS_ROOT_ENV = "SCITEAM_RUNS_ROOT"


def runs_root() -> Path:
    """Directory under which run artifacts are written.

    ``SCITEAM_RUNS_ROOT`` if set, else ``<cwd>/runs`` — matching the
    pre-packaging behaviour for anyone invoking the harness scripts from a
    repository checkout, without ever writing into ``site-packages``.
    """
    raw = os.environ.get(RUNS_ROOT_ENV, "").strip()
    return Path(raw) if raw else Path.cwd() / "runs"
