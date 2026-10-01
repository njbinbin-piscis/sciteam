from pathlib import Path

import yaml

from sciteam.providers.llm.types import (
    AssistantTurn,
    AuthError,
    BadRequest,
    CallParams,
    ChatMessage,
    ContextOverflow,
    LlmError,
    LlmProvider,
    ModelInfo,
    QuotaExhausted,
    RateLimited,
    ToolCall,
    ToolSpec,
    Transient,
    Usage,
)


def load_models(path: Path | str) -> dict[str, ModelInfo]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {name: ModelInfo(name=name, **spec) for name, spec in data.items()}


def make_provider(model: ModelInfo, **kw) -> LlmProvider:
    if model.provider == "openai_compat":
        from sciteam.providers.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(model, **kw)
    raise ValueError(f"unknown provider {model.provider}")


__all__ = [
    "AssistantTurn",
    "AuthError",
    "BadRequest",
    "CallParams",
    "ChatMessage",
    "ContextOverflow",
    "LlmError",
    "LlmProvider",
    "ModelInfo",
    "QuotaExhausted",
    "RateLimited",
    "ToolCall",
    "ToolSpec",
    "Transient",
    "Usage",
    "load_models",
    "make_provider",
]
