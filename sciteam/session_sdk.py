"""``Session`` — the embeddable Python SDK, docs/SESSION-KERNEL.md §8.

A general-purpose, model-agnostic chat+tools session built directly on the
E4/E5 primitives (`sciteam.session`, `sciteam.compaction_port`,
`sciteam.token_meter`). Deliberately independent of the institution kernel
(`mission.py`/`team_loader.py`/seats/authority) — this is the "plain object a
script can create/run/resume/fork/compact/close" that `sciteam.cli` and any
future BFF wrapper are meant to share, not a second loop with its own rules.

`LlmWorkerRuntime` (`llm_worker.py`/`tool_loop.py`) remains the institutional
worker: it additionally enforces seat authority, exit-contract artifacts, and
paradigm-declared tool allowlists. `Session` is for embedding a single
governed-or-ungoverned conversation into a host application without that
machinery — e.g. a probe harness, a notebook, or a future non-institutional
product surface.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sciteam.compaction_port import MechanicalCompactionPort, compact_with_retry_budget
from sciteam.session import FileSessionStore, ProjectedContext, SessionError, project
from sciteam.token_meter import SimpleTokenMeter

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_ROUNDS = 12
DEFAULT_PRESSURE_WINDOW = 100_000
DEFAULT_KEEP_RECENT_TOKENS = 4_000


class ProviderCallable(Protocol):
    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any: ...  # `.content: str | None`, `.tool_calls: list[dict] | None`


ToolHandler = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolRuntime:
    """The tool surface offered to one `Session.run` call. Deliberately a
    plain mapping, not `wizard_tools`'s institution-bound catalog — a host
    application supplies exactly the tools it wants this session to have."""

    definitions: tuple[dict[str, Any], ...] = ()
    handlers: dict[str, ToolHandler] = field(default_factory=dict)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = handler(args)
        except Exception as exc:  # noqa: BLE001 - a broken tool must not crash the session
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return result


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    content: str
    session_id: str
    tool_rounds_used: int


class SessionRuntimeError(SessionError):
    """Raised for `Session`-level failures (exhausted tool rounds, no
    provider attached, etc.) — distinct from the underlying store's
    `SessionError` for integrity failures, but the same base class so a
    caller can catch one type for "this session is broken"."""


class Session:
    """See module docstring and docs/SESSION-KERNEL.md §8 for the contract."""

    def __init__(
        self,
        *,
        store: FileSessionStore,
        session_id: str,
        pins: Sequence[str] = (),
        provider: ProviderCallable | None = None,
        compaction_port: MechanicalCompactionPort | None = None,
        token_meter: SimpleTokenMeter | None = None,
        pressure_window: int = DEFAULT_PRESSURE_WINDOW,
        keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    ) -> None:
        self._store = store
        self.session_id = session_id
        self._pins = tuple(pins)
        self._provider = provider
        self._compaction_port = compaction_port
        self._token_meter = token_meter
        self._pressure_window = pressure_window
        self._keep_recent_tokens = keep_recent_tokens

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def create(
        cls,
        *,
        workspace: Path | str,
        seat: str = "agent",
        pins: Sequence[str] = (),
        session_id: str | None = None,
        **kwargs: Any,
    ) -> Session:
        store = FileSessionStore(workspace)
        sid = session_id or f"{seat}-{uuid.uuid4().hex[:12]}"
        return cls(store=store, session_id=sid, pins=pins, **kwargs)

    @classmethod
    def resume(
        cls,
        *,
        workspace: Path | str,
        session_id: str,
        pins: Sequence[str] = (),
        **kwargs: Any,
    ) -> Session:
        store = FileSessionStore(workspace)
        verdict = store.verify_chain(session_id)
        if not verdict.ok and verdict.length > 0:
            raise SessionRuntimeError(
                f"cannot resume {session_id!r}: chain integrity problems: {verdict.problems}"
            )
        return cls(store=store, session_id=session_id, pins=pins, **kwargs)

    def fork(self, at_seq: int) -> Session:
        new_id = f"{self.session_id}-fork-{uuid.uuid4().hex[:8]}"
        self._store.fork(self.session_id, at_seq, new_id)
        return Session(
            store=self._store,
            session_id=new_id,
            pins=self._pins,
            provider=self._provider,
            compaction_port=self._compaction_port,
            token_meter=self._token_meter,
            pressure_window=self._pressure_window,
            keep_recent_tokens=self._keep_recent_tokens,
        )

    def events(self, since: int = 0):
        return self._store.read_from(self.session_id, since)

    def close(self) -> None:
        self._store.flush(self.session_id)

    # ------------------------------------------------------------------ run

    def _projected(self) -> ProjectedContext:
        return project(list(self._store.read_from(self.session_id)), pins=self._pins)

    async def _maybe_compact(self) -> None:
        if self._compaction_port is None or self._token_meter is None:
            return
        ctx = self._projected()
        reading = self._token_meter.measure(ctx)
        if not self._compaction_port.should_compact(reading, self._pressure_window):
            return
        events = list(self._store.read_from(self.session_id))
        result = await compact_with_retry_budget(
            self._compaction_port,
            events,
            pins=self._pins,
            keep_recent_tokens=self._keep_recent_tokens,
            session_store=self._store,
        )
        if result is not None:
            self._store.append(
                self.session_id,
                "compaction",
                {
                    "trigger": "pressure",
                    "from_seq": result.from_seq,
                    "to_seq": result.to_seq,
                    "summary": result.summary,
                    "summary_ref": result.summary_ref,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                    "pins_preserved": result.pins_preserved,
                },
            )

    async def run(
        self,
        user_input: str,
        *,
        tools: ToolRuntime | None = None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> AssistantTurn:
        if self._provider is None:
            raise SessionRuntimeError(f"session {self.session_id!r} has no provider attached")
        self._store.append(self.session_id, "user_message", {"content": user_input})

        tool_defs = list(tools.definitions) if tools else None
        for round_idx in range(max_tool_rounds + 1):
            await self._maybe_compact()
            ctx = self._projected()
            resp = await self._provider.acomplete(
                list(ctx.messages),
                tools=tool_defs,
                tool_choice="auto" if tool_defs else None,
            )
            tool_calls = getattr(resp, "tool_calls", None) or []
            if not tool_calls:
                content = getattr(resp, "content", "") or ""
                self._store.append(self.session_id, "assistant_message", {"content": content})
                return AssistantTurn(
                    content=content, session_id=self.session_id, tool_rounds_used=round_idx
                )
            self._store.append(
                self.session_id,
                "assistant_message",
                {"content": getattr(resp, "content", None), "tool_calls": tool_calls},
            )
            for tc in tool_calls:
                name = str((tc.get("function") or {}).get("name") or tc.get("name") or "")
                raw_args = (tc.get("function") or {}).get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                call_id = str(tc.get("id") or name)
                self._store.append(
                    self.session_id,
                    "tool_call",
                    {"call_id": call_id, "tool": name, "arguments": args},
                )
                result = tools.call(name, args) if tools is not None else {"ok": False}
                full_text = json.dumps(result, ensure_ascii=False)
                result_ref = self._store.archive(full_text)
                self._store.append(
                    self.session_id,
                    "tool_result",
                    {
                        "call_id": call_id,
                        "tool": name,
                        "result_ref": result_ref,
                        "result_chars": len(full_text),
                        "truncated": False,
                    },
                )
        raise SessionRuntimeError(
            f"session {self.session_id!r} exhausted {max_tool_rounds} tool rounds "
            "without a final answer"
        )
