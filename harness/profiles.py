"""Campaign profiles — adapter packs, not alternate engines.

A profile selects *policy data* and *port implementations* for the shared
Campaign / AdaptiveCampaignPlanner / MissionRunner / LlmWorkerRuntime path.
It must not introduce a parallel orchestration model.

Note: the internal research tree also ships domain-locked benchmark profiles
(mini/covering/pharma — each pairs with a GPU/data-dir-gated frozen eval and
a seed adapter that is not part of this open-source export). Only the
domain-agnostic ``open`` profile is included here; it needs no data dir, no
GPU, and no benchmark-specific eval. Add your own profile the same way: a
``CampaignProfile`` entry plus a ``seed_module`` that returns an initial
plan dict shaped like ``harness.adapters.seed_open_research``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CampaignProfile:
    id: str
    label: str
    seed_module: str
    seed_attr: str
    needs_data_dir: bool
    eval_kind: str  # local_frozen | pharma_gpu | none
    default_goal: str
    notes: str = ""

    def load_seed(self) -> Callable[..., dict[str, Any]]:
        import importlib

        mod = importlib.import_module(self.seed_module)
        fn = getattr(mod, self.seed_attr)
        if not callable(fn):
            raise TypeError(f"{self.seed_module}.{self.seed_attr} is not callable")
        return fn  # type: ignore[return-value]


PROFILES: dict[str, CampaignProfile] = {
    "open": CampaignProfile(
        id="open",
        label="Open research — arbitrary operator goal (default path)",
        seed_module="harness.adapters.seed_open_research",
        seed_attr="seed_open_research_plan",
        needs_data_dir=False,
        eval_kind="none",
        default_goal=(
            "Have the virtual research team scope the operator's scientific "
            "question, propose falsifiable hypotheses, design a method, and "
            "report honestly; the sandbox must never fabricate results it "
            "cannot machine-check."
        ),
        notes=(
            "Thesis path: goal-only launch. No domain frozen eval. "
            "External wet-lab claims must be declared in problem_frame."
        ),
    ),
}


def get_profile(profile_id: str) -> CampaignProfile:
    key = (profile_id or "").strip().lower()
    if key not in PROFILES:
        raise KeyError(f"unknown profile {profile_id!r}; known: {sorted(PROFILES)}")
    return PROFILES[key]


def list_profiles() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "label": p.label,
            "needs_data_dir": p.needs_data_dir,
            "eval_kind": p.eval_kind,
            "default_goal": p.default_goal,
            "seed": f"{p.seed_module}:{p.seed_attr}",
            "notes": p.notes,
            "engine": "shared Campaign + AdaptiveCampaignPlanner + LlmWorkerRuntime",
        }
        for p in PROFILES.values()
    ]
