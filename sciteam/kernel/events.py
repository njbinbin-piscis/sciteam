import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from sciteam.kernel.canonical import canonical_json, format_ts, sha256_hex
from sciteam.kernel.clock import Clock, SystemClock

GENESIS_HASH = "0" * 64
POLL_INTERVAL_S = 0.2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  stream TEXT NOT NULL,
  stream_seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  actor TEXT,
  payload TEXT NOT NULL,
  causation_id INTEGER,
  rule_version TEXT,
  ts TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  UNIQUE (stream, stream_seq)
);
CREATE INDEX IF NOT EXISTS events_type ON events(type);
"""


class ConcurrencyConflict(Exception):
    pass


class IntegrityError(Exception):
    pass


class Event(BaseModel):
    seq: int
    stream: str
    stream_seq: int
    type: str
    actor: str | None
    payload: dict[str, Any]
    causation_id: int | None
    rule_version: str | None
    ts: datetime
    prev_hash: str
    hash: str


class VerifyReport(BaseModel):
    stream: str
    count: int
    ok: bool
    first_bad_stream_seq: int | None = None
    reason: str | None = None


def _event_hash(
    *,
    stream: str,
    stream_seq: int,
    type: str,
    actor: str | None,
    payload: dict[str, Any],
    causation_id: int | None,
    rule_version: str | None,
    ts: str,
    prev_hash: str,
) -> str:
    body = {
        "stream": stream,
        "stream_seq": stream_seq,
        "type": type,
        "actor": actor,
        "payload": payload,
        "causation_id": causation_id,
        "rule_version": rule_version,
        "ts": ts,
        "prev_hash": prev_hash,
    }
    return sha256_hex(canonical_json(body))


_COLUMNS = (
    "seq, stream, stream_seq, type, actor, payload, causation_id, rule_version, ts, "
    "prev_hash, hash"
)


def _row_to_event(row: tuple) -> Event:
    return Event(
        seq=row[0],
        stream=row[1],
        stream_seq=row[2],
        type=row[3],
        actor=row[4],
        payload=json.loads(row[5]),
        causation_id=row[6],
        rule_version=row[7],
        ts=datetime.fromisoformat(row[8].replace("Z", "+00:00")),
        prev_hash=row[9],
        hash=row[10],
    )


class EventStore:
    def __init__(self, path: Path | str, clock: Clock | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(
        self,
        stream: str,
        type: str,
        payload: BaseModel | dict[str, Any],
        *,
        actor: str | None = None,
        causation_id: int | None = None,
        rule_version: str | None = None,
        expected_stream_seq: int | None = None,
    ) -> Event:
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        # Normalize through canonical JSON so the stored payload equals the hashed one.
        payload_json = canonical_json(payload)
        payload = json.loads(payload_json)
        ts = format_ts(self._clock.now())
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    "SELECT stream_seq, hash FROM events WHERE stream=? "
                    "ORDER BY stream_seq DESC LIMIT 1",
                    (stream,),
                ).fetchone()
                tail_seq, prev_hash = (row[0], row[1]) if row else (0, GENESIS_HASH)
                if expected_stream_seq is not None and expected_stream_seq != tail_seq:
                    raise ConcurrencyConflict(
                        f"{stream}: expected tail {expected_stream_seq}, found {tail_seq}"
                    )
                stream_seq = tail_seq + 1
                h = _event_hash(
                    stream=stream,
                    stream_seq=stream_seq,
                    type=type,
                    actor=actor,
                    payload=payload,
                    causation_id=causation_id,
                    rule_version=rule_version,
                    ts=ts,
                    prev_hash=prev_hash,
                )
                cur.execute(
                    "INSERT INTO events (stream, stream_seq, type, actor, payload, causation_id, "
                    "rule_version, ts, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        stream,
                        stream_seq,
                        type,
                        actor,
                        payload_json,
                        causation_id,
                        rule_version,
                        ts,
                        prev_hash,
                        h,
                    ),
                )
                seq = cur.lastrowid
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
        self._notify()
        return Event(
            seq=seq,
            stream=stream,
            stream_seq=stream_seq,
            type=type,
            actor=actor,
            payload=payload,
            causation_id=causation_id,
            rule_version=rule_version,
            ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
            prev_hash=prev_hash,
            hash=h,
        )

    def _notify(self) -> None:
        for loop, ev in list(self._waiters):
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                self._waiters.discard((loop, ev))

    def add_waiter(self, wake: asyncio.Event) -> None:
        """`wake` is set (thread-safely) after every append in this process."""
        self._waiters.add((asyncio.get_running_loop(), wake))

    def remove_waiter(self, wake: asyncio.Event) -> None:
        self._waiters = {w for w in self._waiters if w[1] is not wake}

    def read(self, stream: str, *, after_stream_seq: int = 0) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM events WHERE stream=? AND stream_seq>? "
                "ORDER BY stream_seq",
                (stream, after_stream_seq),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def read_global(
        self,
        *,
        after_seq: int = 0,
        types: set[str] | None = None,
        stream_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        sql = f"SELECT {_COLUMNS} FROM events WHERE seq>?"
        args: list[Any] = [after_seq]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args.extend(sorted(types))
        if stream_prefix:
            sql += " AND substr(stream, 1, ?) = ?"
            args.extend([len(stream_prefix), stream_prefix])
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_event(r) for r in rows]

    async def subscribe(
        self,
        *,
        after_seq: int = 0,
        types: set[str] | None = None,
        stream_prefix: str | None = None,
    ) -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        key = (loop, wake)
        self._waiters.add(key)
        cursor = after_seq
        try:
            while True:
                wake.clear()
                batch = self.read_global(after_seq=cursor, limit=500)
                for ev in batch:
                    cursor = ev.seq
                    if types and ev.type not in types:
                        continue
                    if stream_prefix and not ev.stream.startswith(stream_prefix):
                        continue
                    yield ev
                if batch:
                    continue
                try:
                    await asyncio.wait_for(wake.wait(), timeout=POLL_INTERVAL_S)
                except TimeoutError:
                    pass
        finally:
            self._waiters.discard(key)

    def verify(self, stream: str) -> VerifyReport:
        events = self.read(stream)
        prev = GENESIS_HASH
        for i, ev in enumerate(events, start=1):
            if ev.stream_seq != i:
                return VerifyReport(
                    stream=stream, count=len(events), ok=False,
                    first_bad_stream_seq=ev.stream_seq, reason="stream_seq gap",
                )
            if ev.prev_hash != prev:
                return VerifyReport(
                    stream=stream, count=len(events), ok=False,
                    first_bad_stream_seq=ev.stream_seq, reason="prev_hash mismatch",
                )
            h = _event_hash(
                stream=ev.stream,
                stream_seq=ev.stream_seq,
                type=ev.type,
                actor=ev.actor,
                payload=ev.payload,
                causation_id=ev.causation_id,
                rule_version=ev.rule_version,
                ts=format_ts(ev.ts),
                prev_hash=ev.prev_hash,
            )
            if h != ev.hash:
                return VerifyReport(
                    stream=stream, count=len(events), ok=False,
                    first_bad_stream_seq=ev.stream_seq, reason="hash mismatch",
                )
            prev = ev.hash
        return VerifyReport(stream=stream, count=len(events), ok=True)

    def streams(self, prefix: str = "") -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT stream FROM events WHERE substr(stream, 1, ?) = ? ORDER BY stream",
                (len(prefix), prefix),
            ).fetchall()
        return [r[0] for r in rows]


S = TypeVar("S")


def fold(events: Iterable[Event], reducer: Callable[[S, Event], S], initial: S) -> S:
    state = initial
    for ev in events:
        state = reducer(state, ev)
    return state
