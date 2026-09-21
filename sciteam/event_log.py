"""The institutional ledger: an append-only, hash-chained event stream.

Every other record SciTeam writes — transcripts, traces, artifacts — answers
"what happened". This one answers a different question: **what counted, under
whose authority, against which version of the rules** — and it answers it in a
form a third party can check without trusting the process that produced it.

Three properties make that possible:

* **Append-only.** :class:`EventLog` exposes no update or delete. A mistaken
  entry is corrected by appending a correcting entry, exactly as an
  institution corrects a decision by making another decision.
* **Hash-chained.** Each entry commits to its predecessor, so editing,
  reordering or dropping any entry invalidates every entry after it.
  :func:`verify_chain` reports precisely where the break is.
* **Replayable.** :func:`replay` folds the chain into the institutional state
  it implies — which artifacts took effect, which acts were refused, which
  items were reopened. State is therefore derived from the record rather than
  recorded alongside it, which is what makes the record authoritative.

The writer is deliberately dependency-free and process-local: it reads the
existing file to recover the chain head, so a resumed run continues the same
chain instead of starting a second one. It is *not* safe for two concurrent
writers on one file; a run owns its ledger.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64

LEDGER_FILENAME = "ledger.jsonl"

#: Event kinds the interpreter and :func:`replay` understand. Plugins may emit
#: additional kinds prefixed with ``x_``: they are chained and verified like
#: any other entry, but carry no institutional semantics — a plugin can record
#: an observation without minting a new kind of institutional effect.
CORE_KINDS: frozenset[str] = frozenset(
    {
        "run_started",
        "run_finished",
        "rule_version_set",
        "authority_checked",
        "artifact_submitted",
        "artifact_accepted",
        "artifact_rejected",
        "audit_reported",
        "veto_issued",
        "reopened",
        "closed",
        "amendment_proposed",
        "amendment_decided",
        "amendment_applied",
        "llm_call",
        "tool_call",
    }
)

EFFECTS: frozenset[str] = frozenset({"accepted", "rejected", "observed"})

#: Field order is part of the on-disk contract: readers diffing two ledgers
#: byte-for-byte should not see spurious changes from dict ordering.
_FIELDS = (
    "seq",
    "ts",
    "run_id",
    "kind",
    "seat",
    "authority",
    "effect",
    "rule_version",
    "payload",
    "prev_hash",
    "hash",
)


class LedgerError(Exception):
    """Raised when an entry cannot legally be appended to a chain."""


def _canonical_json(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(record: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of ``record`` with ``hash`` removed."""
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _validate_kind(kind: str) -> None:
    if kind in CORE_KINDS:
        return
    suffix = kind[2:] if kind.startswith("x_") else ""
    if suffix and all(c.islower() or c.isdigit() or c == "_" for c in suffix):
        return
    raise LedgerError(
        f"unknown event kind {kind!r}: use one of the core kinds or an 'x_' prefixed plugin kind"
    )


@dataclass(frozen=True, slots=True)
class InstitutionEvent:
    """A chained ledger entry, as read back from disk."""

    seq: int
    ts: str
    run_id: str
    kind: str
    seat: str | None
    authority: str | None
    effect: str
    rule_version: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> InstitutionEvent:
        required = {"seq", "ts", "run_id", "kind", "effect", "payload", "prev_hash", "hash"}
        missing = required - set(raw)
        if missing:
            raise LedgerError(f"ledger entry is missing required fields: {sorted(missing)}")
        return cls(
            seq=int(raw["seq"]),
            ts=str(raw["ts"]),
            run_id=str(raw["run_id"]),
            kind=str(raw["kind"]),
            seat=raw.get("seat"),
            authority=raw.get("authority"),
            effect=str(raw["effect"]),
            rule_version=raw.get("rule_version"),
            payload=dict(raw["payload"]),
            prev_hash=str(raw["prev_hash"]),
            hash=str(raw["hash"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "kind": self.kind,
            "seat": self.seat,
            "authority": self.authority,
            "effect": self.effect,
            "rule_version": self.rule_version,
            "payload": dict(self.payload),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


class EventLog:
    """Append-only writer/reader for one run's chain.

    Opening an existing file recovers the head, so a resumed run extends the
    same chain. Opening a fresh path starts at the genesis link.
    """

    def __init__(self, path: Path | str, *, run_id: str | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_events(self._path)
        if existing:
            head = existing[-1]
            self._seq = head.seq
            self._head_hash = head.hash
            self._run_id = head.run_id
            if run_id is not None and run_id != self._run_id:
                raise LedgerError(
                    f"ledger at {self._path} belongs to run {self._run_id!r}, not {run_id!r}; "
                    "one chain per run"
                )
        else:
            if run_id is None:
                raise LedgerError("a new ledger needs an explicit run_id")
            self._seq = 0
            self._head_hash = GENESIS_HASH
            self._run_id = run_id
        self._rule_version: str | None = existing[-1].rule_version if existing else None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def head_hash(self) -> str:
        return self._head_hash

    @property
    def length(self) -> int:
        return self._seq

    @property
    def rule_version(self) -> str | None:
        """Rule version currently in force; stamped onto subsequent entries."""
        return self._rule_version

    def set_rule_version(self, rule_version: str, **payload: Any) -> InstitutionEvent:
        """Pin the institution text in force and record the change."""
        event = self.append(
            "rule_version_set",
            effect="observed",
            rule_version=rule_version,
            payload={"previous": self._rule_version, **payload},
        )
        self._rule_version = rule_version
        return event

    def append(
        self,
        kind: str,
        *,
        effect: str = "observed",
        seat: str | None = None,
        authority: str | None = None,
        rule_version: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> InstitutionEvent:
        """Append one entry and return it. The only mutation this class allows."""
        _validate_kind(kind)
        if effect not in EFFECTS:
            raise LedgerError(f"effect must be one of {sorted(EFFECTS)}, got {effect!r}")
        record = {
            "seq": self._seq + 1,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self._run_id,
            "kind": kind,
            "seat": seat,
            "authority": authority,
            "effect": effect,
            "rule_version": rule_version if rule_version is not None else self._rule_version,
            "payload": dict(payload or {}),
            "prev_hash": self._head_hash,
        }
        record["hash"] = entry_hash(record)
        ordered = {k: record[k] for k in _FIELDS}
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ordered, ensure_ascii=False) + "\n")
        self._seq = record["seq"]
        self._head_hash = record["hash"]
        return InstitutionEvent.from_dict(ordered)

    def events(self) -> list[InstitutionEvent]:
        return read_events(self._path)

    def verify(self) -> ChainVerification:
        return verify_chain(self._path)


def read_events(path: Path | str) -> list[InstitutionEvent]:
    """Read a ledger file. Raises :class:`LedgerError` on unreadable lines."""
    path = Path(path)
    if not path.is_file():
        return []
    events: list[InstitutionEvent] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{lineno}: not valid JSON ({exc})") from exc
        events.append(InstitutionEvent.from_dict(raw))
    return events


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Outcome of checking a chain's integrity."""

    ok: bool
    length: int
    head: str
    problems: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ok


def verify_chain(path: Path | str) -> ChainVerification:
    """Check sequence continuity, link integrity and per-entry hashes.

    Reports *every* problem found rather than stopping at the first, so an
    auditor sees the full extent of a break in one pass.
    """
    path = Path(path)
    problems: list[str] = []
    try:
        events = read_events(path)
    except LedgerError as exc:
        return ChainVerification(ok=False, length=0, head=GENESIS_HASH, problems=(str(exc),))

    prev_hash = GENESIS_HASH
    run_id: str | None = None
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            problems.append(f"entry {index}: seq is {event.seq}, expected {index}")
        if event.prev_hash != prev_hash:
            problems.append(
                f"entry {index}: prev_hash {event.prev_hash[:12]}… does not link to "
                f"{prev_hash[:12]}… (entry inserted, removed or reordered)"
            )
        recomputed = entry_hash(event.to_dict())
        if recomputed != event.hash:
            problems.append(
                f"entry {index}: hash mismatch — recorded {event.hash[:12]}…, "
                f"recomputed {recomputed[:12]}… (contents were edited)"
            )
        if run_id is None:
            run_id = event.run_id
        elif event.run_id != run_id:
            problems.append(f"entry {index}: run_id {event.run_id!r} differs from {run_id!r}")
        prev_hash = event.hash

    return ChainVerification(
        ok=not problems,
        length=len(events),
        head=prev_hash,
        problems=tuple(problems),
    )


@dataclass(slots=True)
class LedgerState:
    """Institutional state derived by folding the chain.

    Everything here is *implied by* the record. Nothing is stored separately,
    so the state cannot silently disagree with the evidence behind it.
    """

    run_id: str = ""
    rule_versions: list[str] = field(default_factory=list)
    effective_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    refused_acts: list[dict[str, Any]] = field(default_factory=list)
    vetoes: list[dict[str, Any]] = field(default_factory=list)
    reopenings: list[dict[str, Any]] = field(default_factory=list)
    closed: list[dict[str, Any]] = field(default_factory=list)
    amendments: dict[str, str] = field(default_factory=dict)
    finished: bool = False

    @property
    def rule_version(self) -> str | None:
        return self.rule_versions[-1] if self.rule_versions else None


def replay(events: Iterable[InstitutionEvent] | Path | str) -> LedgerState:
    """Fold a chain into the institutional state it implies."""
    if isinstance(events, (str, Path)):
        events = read_events(events)
    state = LedgerState()
    for event in events:
        if not state.run_id:
            state.run_id = event.run_id
        ref = _subject(event)
        if event.kind == "rule_version_set" and event.rule_version:
            state.rule_versions.append(event.rule_version)
        elif event.effect == "rejected":
            state.refused_acts.append(_act(event))
        elif event.kind == "artifact_accepted":
            state.effective_artifacts[ref] = {
                "seq": event.seq,
                "seat": event.seat,
                "rule_version": event.rule_version,
                **event.payload,
            }
        elif event.kind == "veto_issued":
            state.vetoes.append(_act(event))
            state.effective_artifacts.pop(ref, None)
        elif event.kind == "reopened":
            state.reopenings.append(_act(event))
            state.effective_artifacts.pop(ref, None)
            state.closed = [c for c in state.closed if c["subject"] != ref]
        elif event.kind == "closed":
            state.closed.append(_act(event))
        elif event.kind == "amendment_decided":
            state.amendments[ref] = str(event.payload.get("status") or event.effect)
        elif event.kind == "amendment_applied":
            state.amendments[ref] = "applied"
        elif event.kind == "run_finished":
            state.finished = True
    return state


def _subject(event: InstitutionEvent) -> str:
    payload = event.payload
    for key in ("subject", "artifact_id", "amendment_id", "target", "node_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return f"seq:{event.seq}"


def _act(event: InstitutionEvent) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "kind": event.kind,
        "seat": event.seat,
        "authority": event.authority,
        "subject": _subject(event),
        "reason": event.payload.get("reason"),
    }


@dataclass(frozen=True, slots=True)
class CostLine:
    """Spend attributed to one (seat, model) pair."""

    seat: str
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    usd: float


def cost_report(events: Iterable[InstitutionEvent] | Path | str) -> list[CostLine]:
    """Attribute per-call spend to the seat that incurred it.

    Budget is an institutional resource: knowing a run cost $4.10 is far less
    useful than knowing the Auditor seat burned 70% of it retrying. Reads
    ``llm_call`` entries whose payload carries ``model``, ``usage`` and
    optionally ``usd``; entries without usage contribute a call count only.
    """
    if isinstance(events, (str, Path)):
        events = read_events(events)
    buckets: dict[tuple[str, str], list[float]] = {}
    for event in events:
        if event.kind != "llm_call":
            continue
        key = (event.seat or "<unattributed>", str(event.payload.get("model") or "<unknown>"))
        usage = event.payload.get("usage") or {}
        bucket = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += 1
        bucket[1] += float(usage.get("prompt_tokens") or 0)
        bucket[2] += float(usage.get("completion_tokens") or 0)
        bucket[3] += float(event.payload.get("usd") or 0.0)
    lines = [
        CostLine(
            seat=seat,
            model=model,
            calls=int(calls),
            prompt_tokens=int(prompt),
            completion_tokens=int(completion),
            usd=round(usd, 6),
        )
        for (seat, model), (calls, prompt, completion, usd) in buckets.items()
    ]
    lines.sort(key=lambda line: (-line.usd, -line.calls, line.seat))
    return lines


def iter_kind(events: Iterable[InstitutionEvent], kind: str) -> Iterator[InstitutionEvent]:
    return (event for event in events if event.kind == kind)
