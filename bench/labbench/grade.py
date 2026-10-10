"""External grader for one LAB-Bench item (docs/60 §4.4.1).

Reads the letter the team wrote to /workspace/ANSWER.txt and compares it with the key kept
outside the sandbox. Writes {"item", "choice", "answered", "correct", "score"}: choosing the
"insufficient information" option counts as not answered (precision/coverage, LAB-Bench §3).

Usage: grade.py ITEM_ID WORKSPACE --json OUT
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = HERE / "data" / "keys.json"
ANSWER = "ANSWER.txt"


def grade(item: str, ws: Path) -> dict:
    key = json.loads(KEYS.read_text())[item]
    f = ws / ANSWER
    text = f.read_text() if f.exists() else ""
    m = re.fullmatch(r"\s*\(?([A-Za-z])\)?\.?\s*", text)
    choice = m.group(1).upper() if m else None
    answered = choice is not None and choice != key["insufficient"]
    correct = choice == key["answer"]
    return {"item": item, "choice": choice, "raw": text[:200], "answered": answered,
            "correct": correct, "score": float(correct)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("item")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    a = ap.parse_args()
    r = grade(a.item, a.workspace)
    a.json.write_text(json.dumps(r, indent=2))
    print(json.dumps(r))


if __name__ == "__main__":
    main()
