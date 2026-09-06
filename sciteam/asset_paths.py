"""O_ASSETS_ROOT resolution (ENGINE_ISA §2.10, line E M-E2).

Single source of truth for where institutional assets live. Line E gives each
organization instance its own evolvable asset copy (runs/_org/<arm>/assets/);
a campaign selects it via the SCITEAM_ASSETS_ROOT environment variable. With
the variable unset, behaviour is byte-identical to the single-root era.

Run-scoped `meta.assets_root` (coordinator) keeps the highest precedence and
is not handled here.
"""

from __future__ import annotations

import os
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
ENV_VAR = "SCITEAM_ASSETS_ROOT"


class AssetsRootError(RuntimeError):
    pass


def assets_root() -> Path:
    """Resolve the institutional asset root, fail-fast on a broken override.

    An explicitly configured root must exist and contain a skills/ directory;
    a half-empty copy must never silently degrade into the default root.
    """
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return LAB_ROOT / "assets"
    root = Path(raw)
    if not root.is_dir():
        raise AssetsRootError(f"{ENV_VAR} points to a missing directory: {root}")
    if not (root / "skills").is_dir():
        raise AssetsRootError(f"{ENV_VAR} root lacks a skills/ subdirectory: {root}")
    return root


def skills_dir() -> Path:
    return assets_root() / "skills"


def role_prompts_dir() -> Path:
    return assets_root() / "prompts" / "roles"


def role_catalog_path() -> Path:
    return assets_root() / "roles" / "CATALOG.yaml"


def display_path(path: Path) -> str:
    """Stable human-readable path for manifests: LAB_ROOT-relative when
    possible (default root), otherwise absolute (org asset copies live under
    runs/ or arbitrary directories)."""
    try:
        return str(path.relative_to(LAB_ROOT))
    except ValueError:
        return str(path)
