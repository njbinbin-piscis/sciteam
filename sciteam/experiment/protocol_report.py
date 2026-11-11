"""Paper table of the round protocol (docs/70): `python -m sciteam.experiment.protocol_report <dir>`.

Reads <dir>/<arm>-seed<N>.json (written by run_protocol) and prints a deterministic markdown
table; the same inputs give the same bytes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sciteam.experiment.protocol_metrics import arm_table
from sciteam.experiment.rounds import ProtocolResult

ORDER = ("frozen", "skills", "rules", "linked")


def load(d: Path) -> list[ProtocolResult]:
    out = []
    for p in sorted(d.glob("*-seed*.json")):
        out.append(ProtocolResult(**json.loads(p.read_text())))
    return out


def _cell(m: dict, digits: int = 3) -> str:
    ci = m["ci"]
    base = f"{m['mean']:.{digits}f}"
    return base if ci is None else f"{base} [{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


def table(results: list[ProtocolResult]) -> str:
    t = arm_table(results)
    cols = [("heldout_macro", "held-out"), ("ood_macro", "ood"), ("promoted", "promoted"),
            ("rejected", "rejected"), ("forgetting", "forgetting"),
            ("negative_transfer", "neg. transfer"), ("erroneous_promotions", "wrong promo."),
            ("cost", "cost")]
    lines = ["| arm | orders | " + " | ".join(h for _, h in cols) + " | cost/gain |",
             "|" + "---|" * (len(cols) + 3)]
    for arm in sorted(t, key=lambda a: ORDER.index(a) if a in ORDER else 99):
        row = t[arm]
        cg = "-" if row["cost_per_gain"] is None else f"{row['cost_per_gain']:.2f}"
        lines.append(f"| {arm} | {row['orders']} | "
                     + " | ".join(_cell(row[k]) for k, _ in cols) + f" | {cg} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    d = Path(sys.argv[1])
    out = table(load(d))
    (d / "table.md").write_text(out)
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
