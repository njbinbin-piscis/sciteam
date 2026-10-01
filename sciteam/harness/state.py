"""Activation state, derived only by folding activation events (see apply())."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from sciteam.kernel.blobs import BlobStore
from sciteam.kernel.events import Event
from sciteam.providers.llm.types import AssistantTurn, ChatMessage, ToolCall, Usage

EntryKind = Literal["task", "model", "tool", "notice", "incoming"]
# task/incoming carry real principal input (turn starts); notice is harness scaffolding.
TURN_START_KINDS = ("task", "incoming")


def estimate_message_tokens(m: ChatMessage) -> int:
    n = len(m.content) + len(m.reasoning or "")
    n += sum(len(tc.name) + len(tc.arguments_raw) for tc in m.tool_calls)
    return n // 4 + 4


class Entry(BaseModel):
    idx: int
    kind: EntryKind
    msg: ChatMessage
    tokens: int
    full_output_ref: str | None = None  # tool entries whose content was truncated


class CompactionRecord(BaseModel):
    n: int
    first_kept: int
    folded_from: int
    folded_to: int
    summary: str  # full message text shown to the model
    narrative: str  # summarizer output only; fed back as <previous-summary>
    read_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    method: Literal["llm", "fallback"]
    tokens_before: int
    tokens_after: int


class Totals(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    summary_calls: int = 0

    def add(self, u: Usage, cost: float, *, summary: bool = False) -> None:
        self.prompt_tokens += u.prompt_tokens
        self.completion_tokens += u.completion_tokens
        self.cache_hit_tokens += u.cache_hit_tokens
        self.cache_miss_tokens += u.cache_miss_tokens
        self.reasoning_tokens += u.reasoning_tokens
        self.cost_usd += cost
        if summary:
            self.summary_calls += 1
        else:
            self.model_calls += 1


class ActivationState(BaseModel):
    activation_id: str = ""
    started_mono: float = 0.0
    wall_elapsed_before_resume: float = 0.0
    entries: list[Entry] = Field(default_factory=list)
    pinned: int = 0
    compactions: list[CompactionRecord] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)
    steps: int = 0
    anchor_tokens: int | None = None  # provider-reported context size at anchor_idx
    anchor_idx: int = -1
    notices_sent: set[str] = Field(default_factory=set)
    finish_reviews: int = 0
    finished: dict[str, Any] | None = None
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)

    @property
    def first_kept(self) -> int:
        return self.compactions[-1].first_kept if self.compactions else self.pinned

    @property
    def summary(self) -> CompactionRecord | None:
        return self.compactions[-1] if self.compactions else None

    def add_entry(self, kind: EntryKind, msg: ChatMessage, full_output_ref: str | None = None
                  ) -> Entry:
        e = Entry(idx=len(self.entries), kind=kind, msg=msg, tokens=estimate_message_tokens(msg),
                  full_output_ref=full_output_ref)
        self.entries.append(e)
        return e

    def context_tokens(self, fixed_tokens: int) -> int:
        """Usage-anchored estimate (pi estimateContextTokens): provider count + estimated delta."""
        if self.anchor_tokens is not None and self.anchor_idx >= self.first_kept:
            return self.anchor_tokens + sum(e.tokens for e in self.entries[self.anchor_idx + 1:])
        live = sum(e.tokens for e in self.entries[: self.pinned])
        live += sum(e.tokens for e in self.entries[self.first_kept:])
        if self.summary:
            live += len(self.summary.summary) // 4
        return fixed_tokens + live


def apply(state: ActivationState, ev: Event, blobs: BlobStore) -> ActivationState:
    p = ev.payload
    t = ev.type
    if t == "activation_started":
        state.activation_id = p["activation_id"]
        state.add_entry("task", ChatMessage(role="user", content=blobs.get_text(p["task_ref"])))
        state.pinned = 1
    elif t == "model_returned":
        turn = AssistantTurn.model_validate_json(blobs.get_text(p["turn_ref"]))
        state.add_entry("model", ChatMessage(role="assistant", content=turn.content,
                                             reasoning=turn.reasoning,
                                             tool_calls=turn.tool_calls))
        state.totals.add(turn.usage, p["cost_usd"])
        state.steps += 1
        if not turn.usage.estimated:
            state.anchor_tokens = turn.usage.prompt_tokens + turn.usage.completion_tokens
            state.anchor_idx = len(state.entries) - 1
        state.pending_tool_calls = list(turn.tool_calls)
    elif t == "tool_finished":
        state.add_entry("tool", ChatMessage(role="tool", tool_call_id=p["call_id"],
                                            content=blobs.get_text(p["content_ref"])),
                        full_output_ref=p.get("full_output_ref"))
        state.pending_tool_calls = [c for c in state.pending_tool_calls if c.id != p["call_id"]]
        if (p.get("meta") or {}).get("finish_review"):
            state.finish_reviews += 1
    elif t == "notice_added":
        state.add_entry("notice", ChatMessage(role="user", content=p["text"]))
        if p.get("key"):
            state.notices_sent.add(p["key"])
    elif t == "incoming_added":
        state.add_entry("incoming", ChatMessage(role="user", content=p["text"]))
    elif t == "summary_usage":
        state.totals.add(Usage.model_validate(p["usage"]), p["cost_usd"], summary=True)
    elif t == "compaction_finished":
        rec = CompactionRecord.model_validate(p["record"])
        state.compactions.append(rec)
        state.anchor_tokens = None
        state.anchor_idx = -1
    elif t == "activation_finished":
        state.finished = p
    return state
