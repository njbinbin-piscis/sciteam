"""In-memory team-run store."""

from __future__ import annotations

from sciteam.models import TeamRun


class MemoryStore:
    def __init__(self) -> None:
        self._runs: dict[str, TeamRun] = {}

    def save(self, run: TeamRun) -> None:
        self._runs[run.id] = run

    def get(self, run_id: str) -> TeamRun | None:
        return self._runs.get(run_id)

    def require(self, run_id: str) -> TeamRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(f"unknown team run: {run_id}")
        return run
