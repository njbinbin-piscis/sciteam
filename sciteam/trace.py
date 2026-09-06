"""Campaign / mission trace: the auditable record of how work unfolded.

Feeds the structural metrics (E1 paradigm coverage, E2 re-entry, E3 contract
closure, E4 three-questions) and the ARM evidence bundle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CampaignTrace:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[dict[str, Any]] = []
        if self._path.is_file():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(json.loads(line))

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: str, **payload: Any) -> dict[str, Any]:
        item = {
            "seq": len(self._events) + 1,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            **payload,
        }
        self._events.append(item)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item

    def events(self, event: str | None = None) -> list[dict[str, Any]]:
        if event is None:
            return list(self._events)
        return [e for e in self._events if e.get("event") == event]

    # ---- structural metric exports -------------------------------------

    def paradigms_used(self) -> list[str]:
        seen: list[str] = []
        for item in self._events:
            paradigm = str(item.get("paradigm") or "")
            if paradigm and paradigm not in seen:
                seen.append(paradigm)
        return seen

    def mission_outcomes(self) -> list[dict[str, Any]]:
        return [e for e in self._events if e.get("event") == "mission_finished"]

    def reentries(self) -> list[dict[str, Any]]:
        """Planner decisions that reopened earlier work after a failure (E2)."""
        return [
            e
            for e in self._events
            if e.get("event") == "planner_decision" and e.get("reentry") is True
        ]

    def contract_closure(self) -> dict[str, Any]:
        outcomes = self.mission_outcomes()
        passed = [e for e in outcomes if e.get("contract_ok")]
        failed = [e for e in outcomes if not e.get("contract_ok")]
        return {
            "missions": len(outcomes),
            "contract_passed": len(passed),
            "contract_failed": len(failed),
            "failed_missions": [
                {"mission_id": e.get("mission_id"), "status": e.get("status")} for e in failed
            ],
        }

    def export_summaries(self, out_dir: Path | str) -> dict[str, Path]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        files: dict[str, Path] = {}

        paradigms_file = out / "paradigms_used.json"
        paradigms_file.write_text(
            json.dumps(
                {"paradigms_used": self.paradigms_used(), "count": len(self.paradigms_used())},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        files["paradigms_used"] = paradigms_file

        closure_file = out / "contract_closure.json"
        closure_file.write_text(
            json.dumps(self.contract_closure(), indent=2) + "\n", encoding="utf-8"
        )
        files["contract_closure"] = closure_file

        reentry_file = out / "mission_reentries.json"
        reentry_file.write_text(
            json.dumps({"reentries": self.reentries(), "count": len(self.reentries())}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        files["mission_reentries"] = reentry_file
        return files
