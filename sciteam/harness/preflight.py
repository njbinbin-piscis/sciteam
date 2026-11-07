"""Sandbox Startup Preflight Probe (`sciteam/harness/preflight.py`).

Implements Step S5.5 (derived from TB2 & host disk-exhaustion empirical lessons):
  1. Checks available host/workspace disk space in <10ms (`shutil.disk_usage`) and
     warns or blocks when free disk drops below a configurable safety floor (`min_free_gb`);
  2. Captures a zero-token, 3-line `[Environment Snapshot]` (Python version, available
     scientific/engineering packages, and CLI binaries) so the agent never wastes its
     first 2-3 steps probing `which python3 / pytest / git`.
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class PreflightSnapshot:
    python_version: str
    available_packages: list[str]
    available_binaries: list[str]
    free_disk_gb: float
    disk_healthy: bool
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render_prompt_block(self) -> str:
        pkgs = ", ".join(self.available_packages) if self.available_packages else "stdlib-only"
        bins = ", ".join(self.available_binaries) if self.available_binaries else "basic-posix"
        warn_line = f"\n- WARNING: {self.warning}" if self.warning else ""
        return (
            f"\n## [Environment Snapshot] (0-token preflight)\n"
            f"- Python: `{self.python_version}` | Packages: `{pkgs}`\n"
            f"- CLI Tools: `{bins}` | Free Disk: `{self.free_disk_gb:.1f} GB`"
            f"{warn_line}\n"
        )


class SandboxPreflightProbe:
    """Fast (<50ms), zero-LLM-token environment and disk health probe."""

    CANDIDATE_PACKAGES: tuple[str, ...] = (
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "sympy",
        "sklearn",
        "pytest",
        "pydantic",
    )
    CANDIDATE_BINARIES: tuple[str, ...] = ("git", "python3", "pytest", "uv", "gcc", "curl")

    def __init__(self, *, min_free_gb: float = 0.5, block_on_low_disk: bool = False) -> None:
        self.min_free_gb = float(min_free_gb)
        self.block_on_low_disk = bool(block_on_low_disk)

    def probe(self, workspace_dir: Path | None = None) -> PreflightSnapshot:
        target = Path(workspace_dir) if workspace_dir else Path.cwd()
        target.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(target)
        free_gb = round(usage.free / (1024**3), 2)
        healthy = free_gb >= self.min_free_gb
        warning = ""
        if not healthy:
            warning = (
                f"Low disk space on {target}: {free_gb:.2f} GB free (< {self.min_free_gb:.2f} GB threshold)."
            )
            if self.block_on_low_disk:
                raise RuntimeError(warning)

        pkgs = [
            pkg for pkg in self.CANDIDATE_PACKAGES if importlib.util.find_spec(pkg) is not None
        ]
        bins = [b for b in self.CANDIDATE_BINARIES if shutil.which(b) is not None]
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({platform.machine()})"
        return PreflightSnapshot(
            python_version=py_ver,
            available_packages=pkgs,
            available_binaries=bins,
            free_disk_gb=free_gb,
            disk_healthy=healthy,
            warning=warning,
        )
