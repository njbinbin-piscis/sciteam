#!/usr/bin/env python3
"""Thin shim: the canonical Institution Engineering demo lives in
``sciteam.canonical_demo`` (it ships with the package, so ``sciteam demo``
works from a plain ``pip install``). This script is kept so the command cited
in the paper and README keeps working from a source checkout:

    python harness/run_canonical_institution_demo.py --mode full [--config-swap]
"""

from __future__ import annotations

import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from sciteam.canonical_demo import (  # noqa: E402
    INTERPRETER_FILES,
    build_parser,
    main,
    run,
    run_full,
    run_no_recovery,
    run_no_separation,
    run_prompt_only,
)

__all__ = [
    "INTERPRETER_FILES",
    "build_parser",
    "main",
    "run",
    "run_full",
    "run_no_recovery",
    "run_no_separation",
    "run_prompt_only",
]

if __name__ == "__main__":
    sys.exit(main())
