from pydantic import BaseModel

from sciteam.harness.config import BudgetConfig
from sciteam.harness.state import ActivationState

# budget dimension -> BudgetConfig field holding its base limit
LIMIT_FIELDS = {"tokens": "tokens", "cost": "cost_usd", "wall_clock": "wall_clock_s",
                "steps": "max_steps"}


class BudgetStatus(BaseModel):
    tokens: int
    cost_usd: float
    wall_s: float
    steps: int
    fraction: float
    binding: str  # which limit is closest: tokens | cost | wall_clock | steps
    exhausted: list[str] = []
    limits: dict[str, float] = {}

    def line(self, cfg: BudgetConfig) -> str:
        lim = self.limits
        parts = []
        if lim.get("tokens"):
            parts.append(f"tokens {self.tokens / 1e6:.2f}M / {lim['tokens'] / 1e6:.2f}M")
        if lim.get("cost"):
            parts.append(f"${self.cost_usd:.2f} / ${lim['cost']:.2f}")
        if lim.get("wall_clock"):
            parts.append(f"wall clock {self.wall_s / 60:.0f} / {lim['wall_clock'] / 60:.0f} min")
        if lim.get("steps"):
            parts.append(f"steps {self.steps} / {lim['steps']:.0f}")
        return "budget used: " + ", ".join(parts)


def limits(cfg: BudgetConfig, state: ActivationState) -> dict[str, float]:
    """Base limits plus everything granted so far in this activation; 0 = no limit."""
    out = {}
    for dim, field in LIMIT_FIELDS.items():
        base = float(getattr(cfg, field) or 0)
        out[dim] = base + float(state.budget_extra.get(dim, 0.0)) if base > 0 else 0.0
    return out


def budget_status(cfg: BudgetConfig, state: ActivationState, wall_s: float) -> BudgetStatus:
    lim = limits(cfg, state)
    used = {"tokens": float(state.totals.total_tokens), "cost": state.totals.cost_usd,
            "wall_clock": wall_s, "steps": float(state.steps)}
    parts = {d: used[d] / lim[d] if lim[d] > 0 else 0.0 for d in LIMIT_FIELDS}
    binding = max(parts, key=parts.get)
    return BudgetStatus(tokens=state.totals.total_tokens, cost_usd=state.totals.cost_usd,
                        wall_s=wall_s, steps=state.steps, fraction=parts[binding],
                        binding=binding, exhausted=[d for d, f in parts.items() if f >= 1.0],
                        limits=lim)
