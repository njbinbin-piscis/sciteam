"""Task-Adaptive Execution Profile Router (`sciteam/harness/profile_router.py`).

Implements the 5-gate profile routing protocol from Zhipu AI & Tsinghua's `HarnessPro`
(arXiv:2610.26770 Section 5.3 & Appendix D.1.1):
  - Gate 1 (`compact`): Low marginal value of iteration or no decomposable local oracle.
  - Gate 2 (`research`): Multi-source factual/literature reconstruction or provenance alignment.
  - Gate 3 (`deep`): Strong local deterministic oracle / simulator + constrained synthesis,
    exact mathematical/biochemical/physical invariants, or inverse problems.
  - Gate 4 (`lean`): Bounded deterministic work with <= 2 uncertainties and direct local check.
  - Gate 5 (`standard`): Default for multi-step engineering, debugging, and data workflows.

Also supports tier-specific profile overrides (`lite` / `medium` / `heavy` operating modes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sciteam.harness.config import (
    ExecutionProfilePolicy,
    HarnessConfig,
    RuntimePolicyConfig,
)
from sciteam.kernel.events import EventStore

VALID_PROFILES = ("compact", "lean", "standard", "deep", "research")


@dataclass
class ProfileRouteDecision:
    profile_name: str
    gate_matched: str
    reasoning_effort: str
    economy_after_tools: int
    strict_after_tools: int
    finalize_after_tools: int
    rationale: str
    budget_tier: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "gate_matched": self.gate_matched,
            "reasoning_effort": self.reasoning_effort,
            "economy_after_tools": self.economy_after_tools,
            "strict_after_tools": self.strict_after_tools,
            "finalize_after_tools": self.finalize_after_tools,
            "rationale": self.rationale,
            "budget_tier": self.budget_tier,
        }


def build_tier_policy(
    base_policy: RuntimePolicyConfig | None = None,
    budget_tier: str = "medium",
) -> RuntimePolicyConfig:
    """Construct `lite`, `medium`, or `heavy` RuntimePolicyConfig following HarnessPro Appendix D.1.2."""
    policy = (base_policy or RuntimePolicyConfig()).model_copy(deep=True)
    tier = budget_tier.lower().strip()
    if tier == "lite":
        # HarnessPro Appendix D.1.2 Lite Mode diff: medium effort on deep/research
        if "deep" in policy.profiles:
            policy.profiles["deep"].reasoning_effort = "medium"
        if "research" in policy.profiles:
            policy.profiles["research"].reasoning_effort = "medium"
    elif tier == "heavy":
        # Heavy mode: prioritizes pass rate with higher reasoning effort and generous convergence window
        if "standard" in policy.profiles:
            policy.profiles["standard"].reasoning_effort = "high"
            policy.profiles["standard"].economy_after_tools = 18
            policy.profiles["standard"].strict_after_tools = 32
        if "deep" in policy.profiles:
            policy.profiles["deep"].reasoning_effort = "high"
            policy.profiles["deep"].economy_after_tools = 20
            policy.profiles["deep"].strict_after_tools = 36
    return policy


class ProfileRouter:
    """5-Gate Task-Adaptive Execution Profile Router."""

    def __init__(self, policy: RuntimePolicyConfig | None = None, *, budget_tier: str = "medium",
                 fixed_profile: str | None = None) -> None:
        self.budget_tier = budget_tier.lower().strip() or "medium"
        self.policy = build_tier_policy(policy, self.budget_tier)
        self.fixed_profile = fixed_profile

    def select_profile(
        self,
        task_description: str,
        *,
        available_tools: list[str] | None = None,
        has_local_oracle: bool | None = None,
    ) -> ProfileRouteDecision:
        if self.fixed_profile:
            return self._make_decision(self.fixed_profile, "fixed",
                                       "Profile fixed by the project kind.")
        text = (task_description or "").lower()
        tools = [t.lower() for t in (available_tools or [])]

        # Gate 1: `compact` — explicit low marginal value or unavailable external state / visual-only
        compact_markers = (
            "no local oracle",
            "unavailable credentials",
            "visual judgment only",
            "single-shot status",
            "smoke ping",
        )
        if any(m in text for m in compact_markers) or has_local_oracle is False:
            return self._make_decision(
                "compact",
                "Gate 1 (compact)",
                "Low marginal value of multi-step iteration or no decomposable local oracle.",
            )

        # Gate 2: `research` — multi-source factual/literature reconstruction or provenance alignment
        research_markers = (
            "literature review",
            "multi-source",
            "provenance alignment",
            "historical ranking",
            "survey reconstruction",
            "文献综述",
            "多源检索",
        )
        if any(m in text for m in research_markers):
            return self._make_decision(
                "research",
                "Gate 2 (research)",
                "Primary deliverable requires multi-source factual or scientific literature reconstruction.",
            )

        # Gate 3: `deep` — strong local deterministic oracle/simulator + constrained synthesis / invariants
        deep_markers = (
            "invariant",
            "exact rational",
            "constrained synthesis",
            "molecular breeding",
            "drug discovery",
            "admet",
            "herg",
            "qtl",
            "linkage drag",
            "fem",
            "von mises",
            "hilbert",
            "pell",
            "stam",
            "不变量",
            "精确有理数",
            "分子育种",
            "药物研发",
            "有限元",
            "先导化合物",
        )
        oracle_tools = any(
            any(k in t for k in ("verify", "submit", "docking", "fem", "qtl", "simulate"))
            for t in tools
        )
        if any(m in text for m in deep_markers) or (has_local_oracle is True and oracle_tools):
            # In `lite` budget tier, HarnessPro shifts bounded verifiable tasks toward `lean` or `standard`
            if self.budget_tier == "lite" and len(text) < 280:
                return self._make_decision(
                    "standard",
                    "Gate 3->Lite (standard)",
                    "Lite budget tier down-shifts constrained synthesis task to standard profile to save tokens.",
                )
            return self._make_decision(
                "deep",
                "Gate 3 (deep)",
                "Constrained synthesis / quantitative invariant task with local deterministic oracle.",
            )

        # Gate 4: `lean` — bounded deterministic work with <= 2 uncertainties and direct check
        lean_markers = (
            "simple",
            "single file",
            "format conversion",
            "quick fix",
            "bounded cleanup",
            "单文件",
            "简单转换",
        )
        if any(m in text for m in lean_markers) or (len(text) < 90 and len(tools) <= 3):
            return self._make_decision(
                "lean",
                "Gate 4 (lean)",
                "Bounded deterministic task with <= 2 uncertainties and direct local check.",
            )

        # Gate 5: `standard` — default for ordinary implementation and multi-step workflows
        return self._make_decision(
            "standard",
            "Gate 5 (standard)",
            "Default profile for multi-step engineering, debugging, or analysis workflow.",
        )

    def bind_to_harness_config(
        self,
        cfg: HarnessConfig,
        task_description: str,
        *,
        store: EventStore | None = None,
        stream: str = "session",
        has_local_oracle: bool | None = None,
    ) -> tuple[HarnessConfig, ProfileRouteDecision]:
        """Select a profile for `task_description`, bind it into a copy of `cfg`, and optionally log `profile_selected`."""
        decision = self.select_profile(
            task_description,
            available_tools=cfg.tools,
            has_local_oracle=has_local_oracle,
        )
        active_policy = self.policy.model_copy(deep=True)
        active_policy.active_profile = decision.profile_name
        updated_cfg = cfg.model_copy(
            update={
                "reasoning_effort": decision.reasoning_effort,
                "runtime_policy": active_policy,
            },
            deep=True,
        )
        if store is not None:
            store.append(stream, "profile_selected", decision.to_dict(), actor="profile_router")
        return updated_cfg, decision

    def _make_decision(self, profile_name: str, gate: str, rationale: str) -> ProfileRouteDecision:
        prof: ExecutionProfilePolicy = self.policy.profiles.get(
            profile_name, self.policy.current_profile()
        )
        return ProfileRouteDecision(
            profile_name=profile_name,
            gate_matched=gate,
            reasoning_effort=prof.reasoning_effort,
            economy_after_tools=prof.economy_after_tools,
            strict_after_tools=prof.strict_after_tools,
            finalize_after_tools=prof.finalize_after_tools,
            rationale=rationale,
            budget_tier=self.budget_tier,
        )
