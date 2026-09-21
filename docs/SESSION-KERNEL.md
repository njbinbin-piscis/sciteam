# Session Kernel — frozen contracts (E4)

> Status: frozen design for E4 (see `28-sciteam-productization-roadmap.md` in
> the research tree §E4–E7). This document is Contract-first: the
> dataclasses/Protocols below are the source of truth; implementations in
> `sciteam/session.py`, `sciteam/compaction.py`, `sciteam/token_meter.py`
> must match this file or this file must be amended first.

## 0. Why this exists

Prior to E4, "what the model saw" lived in three places that disagreed under
load: the in-memory `_task_history` dict (`llm_worker.py`), the tool-history
folding inside the same loop (`tool_loop._compact_tool_history`), and the
`inner_loop_*.jsonl` audit trail (`persist.append_inner_loop_trace`, write-only).
None of the three is resumable, none is the model-context source of truth,
and compaction only ever folds tool results — never the growing assistant/user
turns around them. A crash mid-loop loses context; a long research loop grows
unbounded; an installed plugin cannot see or extend any of this because
nothing here is a documented seam.

E4 fixes this by introducing exactly one new persisted object per work item:
the **session log** — and nothing else. It does not replace, and must never
be conflated with, the **institution ledger** (`event_log.py`, ADR-compliant,
records what became institutionally *effective*). The two are correlated by
key, never merged:

```
run_id · mission_id · seat/agent_key · session_id · session_event_seq
        │                                                    │
        └──────────────► InstitutionEvent.payload.session_ref
```

A session event answers "what did the model/tool actually do". An
institution event answers "did that action count". The same physical action
can appear once in the session log and zero or one times in the institution
ledger (a vetoed submission is one session event, one institution event, and
zero institutional effect) — this is the existing "Physical Action ≠
Institutional Effect" thesis, extended down to the transport layer instead of
stopping at the artifact layer.

## 1. `SessionEvent`

Append-only, one file per session (`<mission_workspace>/sessions/<session_id>.jsonl`),
one JSON object per line, newline-terminated, `fsync`-flushed on every append
(see §6 durability). Hash-chained like `event_log.py` for the same reason:
tamper-evidence and torn-write detection are the same problem solved twice
otherwise.

```python
@dataclass(frozen=True)
class SessionEvent:
    seq: int                     # 0-based, strictly increasing, no gaps
    session_id: str
    kind: str                    # see kind vocabulary below
    payload: dict[str, Any]      # kind-specific; never contains raw secrets
    prev_hash: str                # "0"*64 for seq==0
    hash: str                     # sha256(seq|session_id|kind|payload|prev_hash)
    ts: str                       # ISO-8601 UTC
```

### Kind vocabulary (closed set; adding a kind is a contract change)

| kind | payload keys | meaning |
|---|---|---|
| `user_message` | `content` | a user/system turn entering context |
| `assistant_message` | `content, tool_calls?` | a model turn |
| `tool_call` | `tool, arguments, call_id` | model requested a tool |
| `tool_result` | `call_id, tool, result_ref, result_chars, truncated` | tool answered; `result_ref` is a content-addressed pointer (§5), never a bare filesystem path baked into model-visible text |
| `provider_usage` | `provider, model, prompt_tokens, completion_tokens, cached_tokens?, estimated: bool` | one LLM call's accounting; `estimated=false` only when the provider returned real usage |
| `compaction` | `trigger, from_seq, to_seq, summary_ref, tokens_before, tokens_after, pins_preserved: bool` | one compaction pass (§3) |
| `overflow_recovery` | `attempt, provider_error_code, action: "compact_retry"|"give_up"` | provider rejected the request as over-context (§4) |
| `checkpoint` | `cursor_seq, reason` | an explicit durability point a resume can start from |
| `error` | `error_type, message, recoverable: bool` | any failure that entered the session (never silently dropped — this is the same discipline as `AgentOutcome` in `mission.py`) |

Every `kind` except `checkpoint` and `error` must have a projection rule in
§2. This table is exhaustive — an unrecognized `kind` on read is a hard
error (fail-closed), not a skip.

## 2. `SessionStore` (Protocol)

```python
class SessionStore(Protocol):
    def append(self, session_id: str, kind: str, payload: dict[str, Any]) -> SessionEvent: ...
    def read_from(self, session_id: str, cursor_seq: int = 0) -> Iterator[SessionEvent]: ...
    def verify_chain(self, session_id: str) -> ChainVerdict: ...
    def flush(self, session_id: str) -> None: ...
    def fork(self, session_id: str, at_seq: int, new_session_id: str) -> None: ...
```

`FileSessionStore` is the only shipped implementation (single-writer, one
process owns one session file at a time — same limitation `EventLog`
already documents, not a new one). `append` is fsync'd before returning;
a torn last line on read is detected by hash mismatch on the final record
and truncated with a logged `error` event on next open, never silently
accepted (mirrors `event_log.verify_chain`'s tamper-evidence, applied to
crash-induced corruption instead of adversarial tampering).

`fork` copies events `[0, at_seq]` into a new session id and does not touch
the source session. This is exploration history (§7), not an institutional
action, and therefore never appears in `event_log.py`.

## 3. `ContextProjection`

Pure function, no IO: `SessionEvent* → list[ChatMessage]`. Rebuilds exactly
what the provider call must see from the event log plus the latest
compaction's summary. It is deterministic given the same events — replaying
a session must always produce the same projected context, which is the
session-log analogue of `event_log.replay` reconstructing institutional
state.

```python
def project(events: Sequence[SessionEvent], pins: Sequence[str]) -> ProjectedContext:
    """pins are rendered as an unconditional prefix block, never subject to
    the compaction window below — see §3.1."""
```

Tool-call/result pairing is mechanical: a `tool_call` event without a
matching `tool_result` at projection time is either (a) the most recent event
(call in flight — render as pending) or (b) a bug (crash between call and
result) and must render as an explicit `[tool call interrupted]` marker, never
silently vanish and never be presented to the model as if the call never
happened.

### 3.1 Pins — mechanical, not summarizable

Pins are a *closed* list resolved once per work item, never derived from
session content:

- constitution / charter text
- the seat's `authority` tuple and `role`
- the current `institution_version` (from `amendment.py`)
- any open (unresolved) veto or obligation addressed to this seat
- evidence pointers already cited in an artifact this seat is accountable for

Pins are rendered verbatim, outside the compaction window, on every turn.
`CompactionPort` implementations receive pins as an opaque, read-only
argument — the interface has no way to fold, elide or summarize them. This
is the session-layer expression of the same rule `amendment.py`'s
`LAYER_WHITELIST` already enforces for institutional assets: some things
change only through amendment, never through forgetting.

## 4. `TokenMeter`

```python
@dataclass(frozen=True)
class TokenReading:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    estimated: bool          # False only if sourced from provider response usage
    source: str              # "provider" | "heuristic_chars_per_token"

class TokenMeter(Protocol):
    def measure(self, projected: ProjectedContext) -> TokenReading: ...
    def record_call(self, reading: TokenReading) -> None: ...   # feeds provider_usage events
    def pressure(self, reading: TokenReading, window: int) -> float: ...  # 0..1+
```

Never silently treat a character-count heuristic as ground truth: any
reading not sourced from provider-returned usage carries `estimated=True`
and downstream cost/ledger displays must render that flag, not hide it. This
directly targets the honesty gap the E1 productization notes already flagged
for institutional cost accounting (`G2.5`) — applied here to the session
layer's own pressure signal instead of only the institution ledger's per-call
billing.

## 5. `CompactionPort`

```python
class CompactionPort(Protocol):
    def should_compact(self, reading: TokenReading, window: int) -> bool: ...
    def compact(
        self,
        events: Sequence[SessionEvent],
        *,
        pins: Sequence[str],
        keep_recent_tokens: int,
    ) -> CompactionResult | None: ...   # None => no safe range, caller must not retry-loop

@dataclass(frozen=True)
class CompactionResult:
    summary: str
    summary_ref: str          # content-addressed, also retrievable via read_archived_result
    from_seq: int
    to_seq: int
    tokens_before: int
    tokens_after: int         # must be < tokens_before or the result is rejected (non-shrinking guard)
```

Three mechanical stages, run in order, each may make the next stage
unnecessary:

1. **Prune** — deterministic, model-free. Oversized `tool_result` payloads
   beyond `keep_recent_tokens` are replaced by a `result_ref` pointer (this
   already exists as `tool_loop._compact_tool_history`; E4 promotes it to
   stage 1 of this Protocol instead of the loop's only compaction mechanism).
2. **Summarize** — one LLM call over `events[from_seq:to_seq]`, replaying the
   session's own recent tail as context (same warm-cache rationale `pi` and
   `dsh` document), producing `CompactionResult.summary`.
3. **Reject if non-shrinking** — if stage 2's `tokens_after >= tokens_before`,
   the whole compaction attempt is discarded and `should_compact` is asked
   again next turn with a strictly larger `keep_recent_tokens` fallback
   floor; the caller must not loop unboundedly on a broken summarizer (bound:
   `compaction_retries` per event, default 1, mirrors `dsh`'s
   `compactionRetries`).

Every `CompactionResult` that is actually applied is written as one
`compaction` `SessionEvent` — this is the audit trail for "forgetting is
auditable", the requirement flagged during the harness-completeness
discussion.

### 5.1 `read_archived_result` (capability-gated tool)

A pruned `tool_result` is retrievable in the *same* session by a dedicated
tool, gated behind the calling seat's `authority` like every other tool
(`tool_registry.py`'s existing permission model, not a new bypass). Content
is addressed by `result_ref` (sha256 of the original payload), never a raw
filesystem path — so relocating the run directory or replaying it on another
machine does not break the pointer.

## 6. Overflow recovery

```python
class ProviderPort(Protocol):
    ...
    def classify_error(self, exc: Exception) -> ProviderErrorCode: ...

ProviderErrorCode = Literal[
    "context_overflow", "rate_limited", "timeout", "invalid_request",
    "server_error", "unknown",
]
```

On `context_overflow`: caller emits `overflow_recovery(attempt=n, ...)`,
forces one `CompactionPort.compact` pass with a tightened
`keep_recent_tokens` floor (independent of the normal pressure threshold —
this is the `dsh` "canonical overflow bypasses the normal budget" behavior),
retries the provider call once. A second overflow after a compaction that
already shrank the context is `action: "give_up"` and surfaces as a
first-class `AgentOutcome` failure (never a silent empty response) — the
same "no fail-open" discipline `mission.py`'s `ContractValidator` already
applies to malformed artifacts.

## 7. Fork vs. reopen — do not conflate

`SessionStore.fork` (§2) creates exploration history: "try a different
continuation from turn 12". It has no institutional meaning and never
touches `event_log.py`.

`reopen` (`audit_veto.py` / `canonical_demo.py`) is an institutional action:
a veto returns a mission to an earlier legal state, is authorized by a seat's
`authority`, and is itself a ledger event. A session may be forked zero or
many times without any institutional event; a reopen always produces exactly
one. Implementations must not let a plugin or convenience wrapper use one
mechanism to simulate the other.

## 8. `SessionSDK` (Python, headless)

```python
class Session:
    @classmethod
    def create(cls, *, workspace: Path, seat: str, pins: Sequence[str]) -> Session: ...
    @classmethod
    def resume(cls, *, workspace: Path, session_id: str) -> Session: ...
    def fork(self, at_seq: int) -> Session: ...
    async def run(self, user_input: str, *, tools: ToolRuntime) -> AssistantTurn: ...
    async def stream(self, user_input: str, *, tools: ToolRuntime) -> AsyncIterator[SessionEvent]: ...
    def compact(self) -> CompactionResult | None: ...
    def events(self, since: int = 0) -> Iterator[SessionEvent]: ...
    def close(self) -> None: ...   # flushes; does not delete
```

The CLI (`sciteam run`, `sciteam demo`) and the SDK share this class — there
must never be a second, CLI-only loop implementation (this closes the gap
the audit found between `wizard_tools.run_wizard_tool`'s plugin-tool path and
`tool_loop`'s worker path; E4/E-composition unifies both onto one
`ToolRuntime`).

## 9. What this document deliberately does not cover

- Multi-writer / distributed sessions (out of scope per the embedded-headless
  product boundary chosen for this round).
- HTTP/WS transport — `Session` is a plain Python object; a future BFF may
  wrap it, `core`-style, but that wrapper is not part of this package.
- Governed evolution of skills/harness (Line D/E territory) — this document
  freezes the *transport* layer contracts those lines will eventually run
  on top of, nothing more.
