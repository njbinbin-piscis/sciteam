"""Carry an organization's amended configuration into the next run (docs/60 §8).

Only files changed by an enacted amendment are exported, with the text of the final rule
version; every exported file is checked against that version's recorded sha."""

import json
from pathlib import Path

from sciteam.kernel.canonical import sha256_hex
from sciteam.kernel.events import EventStore


def carry(run_dir: Path, dest: Path) -> dict[str, str]:
    store = EventStore(run_dir / "events.db")
    try:
        evs = store.read_global(types={"amendment_enacted", "rule_version_activated"})
    finally:
        store.close()
    paths = {e.payload["path"] for e in evs if e.type == "amendment_enacted"}
    versions = [e for e in evs if e.type == "rule_version_activated"]
    if not versions:
        raise ValueError(f"{run_dir} has no rule version")
    final = versions[-1].payload
    out: dict[str, str] = {}
    for path in sorted(paths):
        text = final["texts"][path]
        if sha256_hex(text) != final["files"][path]:
            raise ValueError(f"{path}: text does not match the final rule version")
        (dest / path).parent.mkdir(parents=True, exist_ok=True)
        (dest / path).write_text(text)
        out[path] = final["files"][path]
    sources = [e.payload for e in evs if e.type == "amendment_enacted"]
    (dest / "CARRIED.json").write_text(json.dumps(
        {"from": str(run_dir), "rule_version": final["version"], "files": out,
         "amendments": sources}, indent=2))
    return out
