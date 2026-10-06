"""Placebo for the auditor-only evidence (docs/60 §6.4): same output format as audit_check.py,
but whether it fails depends only on a hash of solution.py, never on correctness. It tests
whether an extra reject-and-rework step helps by itself. The failure rate matches the share of
screening runs flagged by the real held-out check (34 of 128 = 0.27).
"""

import ast
import hashlib
import sys
from pathlib import Path

SOLUTION = Path("/workspace/solution.py")
P_FAIL = 0.27


def main() -> int:
    src = SOLUTION.read_text() if SOLUTION.exists() else ""
    if not src.strip():
        print("no solution.py")
        return 1
    names = [n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)]
    h = hashlib.sha256(src.encode()).digest()
    fail = names and h[0] / 255 < P_FAIL
    if not fail:
        print(f"held-out check: {len(names)}/{len(names)} steps pass")
        return 0
    bad = names[int.from_bytes(h[1:3], "big") % len(names)]
    print(f"step {bad}: FAIL (AssertionError)")
    print(f"held-out check: {len(names) - 1}/{len(names)} steps pass; failing: {bad}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
