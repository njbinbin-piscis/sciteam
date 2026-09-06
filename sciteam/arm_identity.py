"""Pin-able arm version strings. The own arm must never be the literal ``own``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]


def describe_own_arm(lab_root: Path | None = None) -> str:
    digest = os.environ.get("SCITEAM_IMAGE_DIGEST", "").strip()
    if digest:
        return f"own image:{digest}"
    root = Path(lab_root) if lab_root is not None else LAB_ROOT
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=12"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "own unknown"
    desc = (proc.stdout or "").strip()
    if proc.returncode == 0 and desc:
        return f"own {desc}"
    return "own unknown"


def arm_version(arm: str, runtime: Any) -> str:
    if arm in {"pi", "dsh"}:
        return str(runtime.version())
    return describe_own_arm()
