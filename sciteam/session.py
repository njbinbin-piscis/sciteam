"""The session log: an append-only, hash-chained record of one work item's
conversation with a model and its tools.

See ``docs/SESSION-KERNEL.md`` for the frozen contract this module
implements. In one sentence: the institution ledger (``event_log.py``)
answers "what counted"; this module answers "what the model and tools
actually did" — and the two are correlated by key, never merged.

Deliberately modeled on ``event_log.py``'s hash-chain discipline (append-only,
each entry commits to its predecessor, ``verify_chain`` reports every break
rather than the first) because it solves the same problem — tamper/torn-write
evidence for a record other code will trust — for a second kind of record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
SESSIONS_DIRNAME = "sessions"
ARCHIVE_DIRNAME = "session_archive"

#: Closed kind vocabulary (docs/SESSION-KERNEL.md §1). Adding a kind is a
#: contract change to that document, not a code-only decision.
KINDS: frozenset[str] = frozenset(
    {
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "provider_usage",
        "compaction",
        "overflow_recovery",
        "checkpoint",
        "error",
    }
)

_FIELDS = ("seq", "session_id", "kind", "payload", "prev_hash", "hash", "ts")


class SessionError(Exception):
    """Raised for session log integrity or contract violations."""


def _canonical_json(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _validate_kind(kind: str) -> None:
    if kind not in KINDS:
        raise SessionError(
            f"unknown session event kind {kind!r}; must be one of {sorted(KINDS)} "
            "(docs/SESSION-KERNEL.md §1 is a closed vocabulary — an unrecognized "
            "kind is a hard error, not a skip)"
        )


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """A chained session log entry, as read back from disk."""

    seq: int
    session_id: str
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    ts: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SessionEvent:
        required = {"seq", "session_id", "kind", "payload", "prev_hash", "hash", "ts"}
        missing = required - set(raw)
        if missing:
            raise SessionError(f"session entry missing required fields: {sorted(missing)}")
        return cls(
            seq=int(raw["seq"]),
            session_id=str(raw["session_id"]),
            kind=str(raw["kind"]),
            payload=dict(raw["payload"]),
            prev_hash=str(raw["prev_hash"]),
            hash=str(raw["hash"]),
            ts=str(raw["ts"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "ts": self.ts,
        }


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """Outcome of checking one session's chain integrity."""

    ok: bool
    length: int
    head: str
    problems: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ok


class SessionStore(Protocol):
    """docs/SESSION-KERNEL.md §2."""

    def append(self, session_id: str, kind: str, payload: dict[str, Any]) -> SessionEvent: ...

    def read_from(self, session_id: str, cursor_seq: int = 0) -> Iterator[SessionEvent]: ...

    def verify_chain(self, session_id: str) -> ChainVerdict: ...

    def flush(self, session_id: str) -> None: ...

    def fork(self, session_id: str, at_seq: int, new_session_id: str) -> None: ...


def _read_events_tolerant(path: Path) -> tuple[list[SessionEvent], bool]:
    """Read a session file, truncating a torn (incomplete/corrupt) last line.

    Returns ``(events, was_torn)``. Unlike ``event_log.read_events`` (which
    treats any bad line as an adversarial-tampering error), a session file's
    *last* line can legitimately be torn by a crash mid-``write`` — this is
    expected, not adversarial, and is repaired by truncation plus a logged
    ``error`` event on next append, never by silently dropping the file or
    raising out of a resume path.
    """
    if not path.is_file():
        return [], False
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[SessionEvent] = []
    torn = False
    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            event = SessionEvent.from_dict(raw)
        except (json.JSONDecodeError, SessionError) as exc:
            if lineno == len(lines):
                torn = True
                break
            raise SessionError(
                f"{path}:{lineno}: corrupt session entry (not the last line)"
            ) from exc
        events.append(event)
    return events, torn


class FileSessionStore:
    """Single-writer, file-per-session store (docs/SESSION-KERNEL.md §2).

    One process owns one session file at a time — the same limitation
    ``event_log.EventLog`` already documents for the institution ledger,
    not a new one introduced here.
    """

    def __init__(self, workspace: Path | str) -> None:
        self._workspace = Path(workspace)
        self._sessions_dir = self._workspace / SESSIONS_DIRNAME
        self._archive_dir = self._workspace / ARCHIVE_DIRNAME
        self._heads: dict[str, tuple[int, str]] = {}

    def _path(self, session_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self._sessions_dir / f"{safe}.jsonl"

    def _load_head(self, session_id: str) -> tuple[int, str]:
        if session_id in self._heads:
            return self._heads[session_id]
        path = self._path(session_id)
        events, torn = _read_events_tolerant(path)
        seq = events[-1].seq if events else 0
        head = events[-1].hash if events else GENESIS_HASH
        if torn:
            logger.warning(
                "session %s: truncated a torn last line on open (path=%s); "
                "this is expected after a crash mid-write, not adversarial tampering",
                session_id,
                path,
            )
            self._rewrite(path, events)
        self._heads[session_id] = (seq, head)
        return seq, head

    @staticmethod
    def _rewrite(path: Path, events: Sequence[SessionEvent]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def append(self, session_id: str, kind: str, payload: dict[str, Any]) -> SessionEvent:
        _validate_kind(kind)
        seq, head = self._load_head(session_id)
        record = {
            "seq": seq + 1,
            "session_id": session_id,
            "kind": kind,
            "payload": dict(payload),
            "prev_hash": head,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        record["hash"] = entry_hash(record)
        ordered = {k: record[k] for k in _FIELDS}
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ordered, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._heads[session_id] = (record["seq"], record["hash"])
        return SessionEvent.from_dict(ordered)

    def read_from(self, session_id: str, cursor_seq: int = 0) -> Iterator[SessionEvent]:
        events, _ = _read_events_tolerant(self._path(session_id))
        for event in events:
            if event.seq > cursor_seq:
                yield event

    def verify_chain(self, session_id: str) -> ChainVerdict:
        events, torn = _read_events_tolerant(self._path(session_id))
        problems: list[str] = []
        if torn:
            problems.append("last line was torn (incomplete write); truncated on read")
        prev_hash = GENESIS_HASH
        for index, event in enumerate(events, start=1):
            if event.seq != index:
                problems.append(f"entry {index}: seq is {event.seq}, expected {index}")
            if event.prev_hash != prev_hash:
                problems.append(f"entry {index}: prev_hash {event.prev_hash[:12]}... does not link")
            recomputed = entry_hash(event.to_dict())
            if recomputed != event.hash:
                problems.append(f"entry {index}: hash mismatch (contents were edited)")
            prev_hash = event.hash
        return ChainVerdict(
            ok=not problems, length=len(events), head=prev_hash, problems=tuple(problems)
        )

    def flush(self, session_id: str) -> None:
        # append() already fsyncs every write; flush() exists as an explicit
        # durability checkpoint call site for callers (docs/SESSION-KERNEL.md §8).
        self._load_head(session_id)

    def fork(self, session_id: str, at_seq: int, new_session_id: str) -> None:
        """Copy events [0, at_seq] into a new session id.

        This is exploration history (docs/SESSION-KERNEL.md §7): it never
        touches the source session and never produces an institution ledger
        event. A reopen is a different, institutional mechanism — see
        ``audit_veto.py`` — and must not be simulated with this method.
        """
        if new_session_id == session_id:
            raise SessionError("fork target must differ from the source session id")
        source_events, _ = _read_events_tolerant(self._path(session_id))
        kept = [e for e in source_events if e.seq <= at_seq]
        if at_seq > 0 and (not kept or kept[-1].seq != at_seq):
            raise SessionError(f"fork point seq={at_seq} does not exist in session {session_id}")
        dest_path = self._path(new_session_id)
        if dest_path.exists():
            raise SessionError(f"fork target session {new_session_id!r} already exists")
        renumbered: list[SessionEvent] = []
        prev_hash = GENESIS_HASH
        for new_seq, event in enumerate(kept, start=1):
            record = {
                "seq": new_seq,
                "session_id": new_session_id,
                "kind": event.kind,
                "payload": dict(event.payload),
                "prev_hash": prev_hash,
                "ts": event.ts,
            }
            record["hash"] = entry_hash(record)
            ordered = {k: record[k] for k in _FIELDS}
            renumbered.append(SessionEvent.from_dict(ordered))
            prev_hash = record["hash"]
        self._rewrite(dest_path, renumbered)
        self._heads[new_session_id] = (
            (renumbered[-1].seq, renumbered[-1].hash) if renumbered else (0, GENESIS_HASH)
        )

    # ---------------------------------------------------------- content archive

    def archive(self, content: str) -> str:
        """Content-address ``content`` and persist it; return its ``result_ref``.

        Used by tool-result pruning (docs/SESSION-KERNEL.md §5.1) so a folded
        pointer survives a run-directory move or a replay on another machine
        — the pointer is a hash, never a raw filesystem path baked into
        model-visible text.
        """
        digest = hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        path = self._archive_dir / f"{digest}.txt"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        return digest

    def read_archived(self, result_ref: str) -> str:
        path = self._archive_dir / f"{result_ref}.txt"
        if not path.is_file():
            raise SessionError(f"no archived content for result_ref={result_ref!r}")
        return path.read_text(encoding="utf-8")


# --------------------------------------------------------------- projection


@dataclass(frozen=True, slots=True)
class ProjectedContext:
    """The model-visible context rebuilt from a session's events + pins."""

    messages: tuple[dict[str, Any], ...]
    pins: tuple[str, ...]
    latest_compaction_seq: int | None


def project(
    events: Sequence[SessionEvent],
    pins: Sequence[str] = (),
) -> ProjectedContext:
    """docs/SESSION-KERNEL.md §3. Pure, deterministic: same events => same
    projection. Tool-call/result pairing is mechanical — an unanswered call
    at the end of the window renders as pending; one that is not the most
    recent event is a crash-between-call-and-result and renders as an
    explicit interrupted marker rather than silently vanishing.
    """
    latest_compaction: SessionEvent | None = None
    for event in events:
        if event.kind == "compaction":
            latest_compaction = event

    window = events
    if latest_compaction is not None:
        window = [e for e in events if e.seq > latest_compaction.seq]

    messages: list[dict[str, Any]] = []
    if pins:
        messages.append({"role": "system", "content": "\n\n".join(pins), "pinned": True})
    if latest_compaction is not None:
        messages.append(
            {
                "role": "system",
                "content": str(latest_compaction.payload.get("summary") or ""),
                "compaction_ref": latest_compaction.payload.get("summary_ref"),
            }
        )

    pending_calls: dict[str, dict[str, Any]] = {}
    for event in window:
        if event.kind == "user_message":
            messages.append({"role": "user", "content": event.payload.get("content", "")})
        elif event.kind == "assistant_message":
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": event.payload.get("content"),
            }
            tool_calls = event.payload.get("tool_calls")
            if tool_calls:
                msg["tool_calls"] = tool_calls
                for tc in tool_calls:
                    call_id = str(tc.get("id") or tc.get("call_id") or "")
                    if call_id:
                        pending_calls[call_id] = tc
            messages.append(msg)
        elif event.kind == "tool_call":
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                pending_calls[call_id] = event.payload
        elif event.kind == "tool_result":
            call_id = str(event.payload.get("call_id") or "")
            pending_calls.pop(call_id, None)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": event.payload.get("result_ref", ""),
                    "truncated": bool(event.payload.get("truncated", False)),
                }
            )
        elif event.kind == "error":
            messages.append(
                {"role": "system", "content": f"[error] {event.payload.get('message', '')}"}
            )
        # provider_usage / overflow_recovery / checkpoint carry no model-visible turn.

    is_last_event_a_call = bool(window) and window[-1].kind in ("assistant_message", "tool_call")
    for call_id in list(pending_calls):
        if is_last_event_a_call and call_id in pending_calls:
            # Most recent event is an in-flight call — leave it pending, not
            # an error; the caller has simply not received an answer yet.
            continue
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "[tool call interrupted]",
                "interrupted": True,
            }
        )

    return ProjectedContext(
        messages=tuple(messages),
        pins=tuple(pins),
        latest_compaction_seq=latest_compaction.seq if latest_compaction is not None else None,
    )
