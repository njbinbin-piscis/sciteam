from pydantic import BaseModel, Field


class ToolOutputConfig(BaseModel):
    max_chars: int = 30_000
    head_fraction: float = 0.4
    parallel_readonly: int = 8
    read_file_default_lines: int = 2000
    read_file_max_line_chars: int = 2000


class BudgetConfig(BaseModel):
    cost_usd: float = 50.0
    wall_clock_s: float = 10_800
    max_steps: int = 3000
    warn_at: float = 0.70
    decide_at: float = 0.85
    status_every_steps: int = 20


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
