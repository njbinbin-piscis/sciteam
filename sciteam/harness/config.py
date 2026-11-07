from typing import Literal

from pydantic import BaseModel, Field


class ToolOutputConfig(BaseModel):
    max_chars: int = 4000
    head_fraction: float = 0.55
    parallel_readonly: int = 8
    read_file_default_lines: int = 2000
    read_file_max_line_chars: int = 2000


class BudgetConfig(BaseModel):
    """Per-activation limits; 0 disables a limit. With `on_exhausted: pause` a reached limit is a
    checkpoint: the activation waits for a grant (owner or auto policy) and then continues with
    the same context; `handoff` ends it with a closing call."""
    tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 10_800
    max_steps: int = 3000
    warn_at: float = 0.70
    decide_at: float = 0.85
    status_every_steps: int = 20
    on_exhausted: Literal["handoff", "pause"] = "handoff"


class CompactionConfig(BaseModel):
    enabled: bool = True
    trigger_fraction: float = 0.5
    trigger_abs_cap: int = 400_000
    tail_fraction: float = 0.025
    tail_min: int = 10_000
    tail_max: int = 25_000
    tail_max_window_fraction: float = 0.20
    summary_model: str | None = None
    summary_reasoning_effort: str | None = "high"
    summary_max_output: int = 16_000
    serialize_tool_result_chars: int = 2000
    serialize_thinking_chars: int = 2000
    anchor_budget_chars: int = 7000
    verbatim_user_budget_chars: int = 24_000
    verbatim_user_message_max_chars: int = 4000
    cooldown_s: list[float] = Field(default_factory=lambda: [60, 300, 900])


class RecoveryConfig(BaseModel):
    provider_retries: int = 8
    backoff_base_s: float = 2.0
    backoff_max_s: float = 120.0
    loop_window: int = 6
    loop_repeat: int = 3
    empty_turn_retries: int = 2
    length_retries: int = 2


class SandboxConfig(BaseModel):
    kind: str = "docker"
    network: str = "none"
    cmd_default_timeout_s: float = 900
    cmd_max_timeout_s: float = 3600


class ExecutionProfilePolicy(BaseModel):
    """Per-profile execution policy from HarnessPro (arXiv:2610.26770 Appendix D.1)."""

    reasoning_effort: str = "medium"
    economy_after_tools: int = 14
    strict_after_tools: int = 26
    finalize_after_tools: int = 0
    strict_reminder_every: int = 8


class RuntimePolicyToolOutput(BaseModel):
    history_token_limit: int = 900
    max_chars: int = 4000
    head_chars: int = 2400
    tail_chars: int = 900
    rtk_enabled: bool = True


class RuntimePolicyConfig(BaseModel):
    """Runtime execution policy (`runtime_policy.json`) evolved by HarnessPro."""

    default_profile: str = "standard"
    active_profile: str | None = None
    tool_output: RuntimePolicyToolOutput = Field(default_factory=RuntimePolicyToolOutput)
    profiles: dict[str, ExecutionProfilePolicy] = Field(
        default_factory=lambda: {
            "compact": ExecutionProfilePolicy(
                reasoning_effort="low",
                economy_after_tools=2,
                strict_after_tools=4,
                finalize_after_tools=0,
                strict_reminder_every=2,
            ),
            "lean": ExecutionProfilePolicy(
                reasoning_effort="low",
                economy_after_tools=6,
                strict_after_tools=10,
                finalize_after_tools=0,
                strict_reminder_every=4,
            ),
            "standard": ExecutionProfilePolicy(
                reasoning_effort="medium",
                economy_after_tools=14,
                strict_after_tools=26,
                finalize_after_tools=0,
                strict_reminder_every=8,
            ),
            "deep": ExecutionProfilePolicy(
                reasoning_effort="high",
                economy_after_tools=16,
                strict_after_tools=30,
                finalize_after_tools=0,
                strict_reminder_every=8,
            ),
            "research": ExecutionProfilePolicy(
                reasoning_effort="high",
                economy_after_tools=8,
                strict_after_tools=20,
                finalize_after_tools=0,
                strict_reminder_every=6,
            ),
        }
    )

    def current_profile(self) -> ExecutionProfilePolicy:
        name = self.active_profile or self.default_profile
        return self.profiles.get(name) or self.profiles.get("standard") or ExecutionProfilePolicy()


class DelegateConfig(BaseModel):
    """What a sub-agent started by `delegate` may use: the tools it may be given (a subset of
    its parent's) and its own budget, capped per call."""
    tools: list[str] = Field(default_factory=lambda: [
        "bash", "read_file", "write_file", "edit_file", "read_output", "glob", "grep",
        "python_repl", "inspect_dataset"])
    tokens: int = 600_000
    max_steps: int = 80
    wall_clock_s: float = 1800
    per_activation: int = 8
    summary_max_chars: int = 6000


class HarnessConfig(BaseModel):
    name: str
    model: str
    reasoning_effort: str | None = None
    system_prompt_ref: str = "prompts/coder.md"
    tools: list[str] = Field(
        default_factory=lambda: ["bash", "read_file", "write_file", "edit_file", "recall",
                                 "read_output", "finish"]
    )
    call_timeout_s: float = 3600
    max_output_tokens: int | None = None
    # Times a finish(completed) is returned for a requirement-by-requirement check before being
    # accepted (docs/20-harness.md §7). Not applied to the budget closing call.
    finish_reviews: int = 0
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    runtime_policy: RuntimePolicyConfig | None = None
    delegate: DelegateConfig = Field(default_factory=DelegateConfig)
