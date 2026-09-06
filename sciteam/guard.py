"""Governance clamp: templates may only tighten concurrency / budgets."""

from __future__ import annotations

from sciteam.coordination import CoordinationSpec


class TeamGuard:
    def __init__(
        self,
        *,
        hard_max_parallel: int = 8,
        hard_max_total_rounds: int = 0,
        hard_max_replicas: int = 8,
    ) -> None:
        self.hard_max_parallel = max(1, hard_max_parallel)
        self.hard_max_total_rounds = max(0, hard_max_total_rounds)
        self.hard_max_replicas = max(0, hard_max_replicas)

    def effective_max_parallel(self, spec: CoordinationSpec) -> int:
        template = spec.scheduling.max_parallel
        if template <= 0:
            return self.hard_max_parallel
        return min(template, self.hard_max_parallel)

    def effective_max_total_rounds(self, spec: CoordinationSpec) -> int:
        template = spec.stopping.max_total_rounds or spec.stopping.max_iterations
        hard = self.hard_max_total_rounds
        if template <= 0:
            return hard
        if hard <= 0:
            return template
        return min(template, hard)

    def effective_max_replicas(self, spec: CoordinationSpec) -> int:
        template = spec.max_replicas
        if template <= 0:
            return self.hard_max_replicas
        if self.hard_max_replicas <= 0:
            return template
        return min(template, self.hard_max_replicas)
