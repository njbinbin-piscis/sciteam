from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from sciteam.kernel.blobs import BlobRef


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments_raw: str
    arguments: dict[str, Any] | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False


class AssistantTurn(BaseModel):
    content: str = ""
    reasoning: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    raw_ref: BlobRef | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    reasoning: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class CallParams(BaseModel):
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    tool_choice: str | dict[str, Any] = "auto"
    timeout_s: float = 1800.0
    seed: int | None = None


class PriceTier(BaseModel):
    cache_hit: float
    cache_miss: float
    output: float


class ReasoningInfo(BaseModel):
    supported: bool = False
    efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None
    passback_with_tools: bool = False


class ModelInfo(BaseModel):
    name: str
    provider: str
    base_url: str
    api_key_env: str
    wire_model: str
    context_window: int
    max_output_tokens: int
    reasoning: ReasoningInfo = Field(default_factory=ReasoningInfo)
    pricing_usd_per_mtok: dict[str, PriceTier]
    peak_hours_utc: list[str] = Field(default_factory=list)
    peak_weekdays_only: bool = True
    concurrency_limit: int = 16
    forced_tool_choice: bool = True


class LlmError(Exception):
    category = "error"

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RateLimited(LlmError):
    category = "rate_limited"


class Transient(LlmError):
    category = "transient"


class ContextOverflow(LlmError):
    category = "context_overflow"


class BadRequest(LlmError):
    category = "bad_request"


class AuthError(LlmError):
    category = "auth"


class QuotaExhausted(LlmError):
    """Account balance or quota exhausted: nothing in this activation can succeed until topped up."""

    category = "provider_quota"


# Activation outcomes caused by the provider account, not by the agent: every further call
# fails the same way, so the run halts instead of letting the agents fall silent.
HALT_REASONS = frozenset({QuotaExhausted.category, AuthError.category})


class LlmProvider(Protocol):
    model_info: ModelInfo

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec], *, params: CallParams
    ) -> AssistantTurn: ...
