from pydantic import BaseModel

from sciteam.harness.config import BudgetConfig
from sciteam.harness.state import ActivationState


class BudgetStatus(BaseModel):
    cost_usd: float
    wall_s: float
    steps: int
    fraction: float
    binding: str  # which limit is closest: cost | wall_clock | steps

    def line(self, cfg: BudgetConfig) -> str:
        return (f"budget used: ${self.cost_usd:.2f} / ${cfg.cost_usd:.2f}, "
                f"wall clock {self.wall_s / 60:.0f} / {cfg.wall_clock_s / 60:.0f} min, "
                f"steps {self.steps} / {cfg.max_steps}")


def budget_status(cfg: BudgetConfig, state: ActivationState, wall_s: float) -> BudgetStatus:
    parts = {
        "cost": state.totals.cost_usd / cfg.cost_usd if cfg.cost_usd > 0 else 0.0,
        "wall_clock": wall_s / cfg.wall_clock_s if cfg.wall_clock_s > 0 else 0.0,
        "steps": state.steps / cfg.max_steps if cfg.max_steps > 0 else 0.0,
    }
    binding = max(parts, key=parts.get)
    return BudgetStatus(cost_usd=state.totals.cost_usd, wall_s=wall_s, steps=state.steps,
                        fraction=parts[binding], binding=binding)
