"""Shared fixtures for the confkit hidden acceptance suite.

The package under test is located via PYTHONPATH (set by score.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _package_root() -> str:
    import confkit

    return str(Path(confkit.__file__).resolve().parent.parent)


@pytest.fixture
def cli(tmp_path):
    """Run `python -m confkit ...` in tmp_path; returns a function(args, env_extra=None)."""

    def run(args, env_extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CONFKIT_")}
        env["PYTHONPATH"] = _package_root()
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "confkit", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run


@pytest.fixture
def write(tmp_path):
    """Write a file into tmp_path and return its name (relative to tmp_path)."""

    def _write(name: str, content: str) -> str:
        (tmp_path / name).write_text(content, encoding="utf-8")
        return name

    return _write
