"""Grundnorm G7 amendment kernel (line E).

Mechanical core of the amendment procedure: fail-closed layer derivation,
schema preflight, append-only ledger with a status state machine, strict
unified-diff application with post-apply loadability checks, institution
version hashing, and motivation-alignment audit.

Pure functions + file IO only: no LLM calls, no orchestration imports, so the
same kernel serves fake/scripted tests and live campaign runners alike.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema
import yaml


class AmendmentError(Exception):
    pass


class LedgerError(AmendmentError):
    pass


class ApplyError(AmendmentError):
    pass


# ---------------------------------------------------------------------------
# Layer whitelist (Grundnorm G7.3, frozen in lineE/plans/M-E0-plan.md §4;
# extended by lineF/plans/M-F1-plan.md §4 — see that table for the line F
# addendum, which does not alter any pre-existing prefix's semantics).
# Order matters: exact-file entries must precede their directory prefixes.
# Anything not matched here is rejected (fail-closed).
# ---------------------------------------------------------------------------
LAYER_WHITELIST: tuple[tuple[str, str], ...] = (
    ("prompts/constitution.md", "C"),
    ("roles/CATALOG.yaml", "L"),
    ("institutions/", "G"),
    ("skills/", "P"),
    ("prompts/roles/", "P"),
    ("prompts/tasks/", "L"),
    ("teams/paradigms/", "L"),
    ("templates/", "L"),
    ("campaigns/", "L"),
    ("breeding/", "L"),  # line F: fitness_criteria and related breeding assets
)

_LOADABLE_SUFFIXES = {".yaml", ".yml", ".json", ".md", ".txt"}

# Ledger status state machine. `None` = no prior entry for this amendment_id.
_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"drafted", "ungoverned_applied"},
    "drafted": {"shadow_pass", "shadow_fail", "rejected", "entrenchment_blocked"},
    "shadow_pass": {"promoted", "rejected"},
    "promoted": {"rolled_back"},
}
_TERMINAL = {"shadow_fail", "rejected", "entrenchment_blocked", "rolled_back", "ungoverned_applied"}


def derive_layer(target_asset: str) -> str | None:
    """Map an asset path (relative to the org asset root) to its layer.

    Returns "P" | "L" | "C" | "G", or None when the path is outside the
    whitelist or structurally unsafe (absolute, backslashes, `..` traversal).
    """
    if not target_asset or "\\" in target_asset:
        return None
    pure = PurePosixPath(target_asset)
    if pure.is_absolute() or ".." in pure.parts or target_asset.startswith("./"):
        return None
    normalized = str(pure)
    for prefix, layer in LAYER_WHITELIST:
        if prefix.endswith("/"):
            if normalized.startswith(prefix):
                return layer
        elif normalized == prefix:
            return layer
    return None


@dataclass
class PreflightResult:
    ok: bool
    layer: str | None = None
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _breeding_read_only_seats(assets_root: Path | str) -> frozenset[str]:
    """Line F, M-F4/F3-c fix (2026-08-15): `fitness_criteria.yaml`'s
    `read_only_for` field was a pure *declaration* until now — nothing in
    `sciteam/`/`harness/` ever read it (M-F4-plan.md §2.3 mechanism check:
    `grep -rl breeding|breeder|fitness_criteria sciteam/ harness/` had zero
    hits outside the asset itself). The only protection breeding assets had
    was the generic L-layer amendment procedure (same gate as any other L
    asset, no seat-awareness). This makes the declaration load-bearing: a
    proposer seat listed in `read_only_for` is rejected at preflight for any
    `breeding/` target, regardless of how well-formed the diff/motivation
    otherwise is. Fails open (empty set) when the file is absent or
    malformed — this is a seat-level *tightening* on top of the existing
    fail-closed layer whitelist, not a replacement for it."""
    path = Path(assets_root) / "breeding" / "fitness_criteria.yaml"
    if not path.is_file():
        return frozenset()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    seats = data.get("read_only_for")
    if not isinstance(seats, list):
        return frozenset()
    return frozenset(str(s) for s in seats)


def preflight(
    proposal: dict[str, Any],
    schemas_dir: Path | str,
    *,
    assets_root: Path | str | None = None,
) -> PreflightResult:
    """Validate shape, derive the layer fail-closed, reject G / off-whitelist.

    `assets_root`, when given, additionally enforces `breeding/`'s seat-level
    `read_only_for` list (F3-c fix, see `_breeding_read_only_seats`). Optional
    and keyword-only so existing callers/tests that only exercise the
    layer/schema gate are unaffected.
    """
    schema_path = Path(schemas_dir) / "amendment_proposal.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(proposal), key=lambda e: list(e.absolute_path))
    if errors:
        reasons = [
            f"schema:{'/'.join(str(p) for p in e.absolute_path) or '<root>'}:{e.message}"
            for e in errors
        ]
        return PreflightResult(ok=False, reasons=reasons)

    layer = derive_layer(proposal["target_asset"])
    if layer is None:
        return PreflightResult(ok=False, reasons=["off_whitelist"])
    if layer == "G":
        return PreflightResult(ok=False, layer="G", reasons=["entrenchment"])

    if assets_root is not None and proposal["target_asset"].startswith("breeding/"):
        read_only_seats = _breeding_read_only_seats(assets_root)
        if str(proposal.get("proposer")) in read_only_seats:
            return PreflightResult(ok=False, layer=layer, reasons=["breeding_seat_read_only"])

    flags: list[str] = []
    declared = proposal.get("declared_layer")
    if declared is not None and declared != layer:
        flags.append(f"declared_layer_mismatch:declared={declared},derived={layer}")
    return PreflightResult(ok=True, layer=layer, flags=flags)


def ledger_path(org_dir: Path | str) -> Path:
    return Path(org_dir) / "amendments.jsonl"


def read_ledger(org_dir: Path | str) -> list[dict[str, Any]]:
    path = ledger_path(org_dir)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _last_status(entries: list[dict[str, Any]], amendment_id: str) -> str | None:
    status = None
    for entry in entries:
        if entry.get("amendment_id") == amendment_id:
            status = entry.get("status")
    return status


def append_ledger(
    org_dir: Path | str,
    entry: dict[str, Any],
    *,
    governance: str = "governed",
) -> dict[str, Any]:
    """Append one status-transition record; enforce the G7 state machine."""
    amendment_id = entry.get("amendment_id")
    status = entry.get("status")
    if not amendment_id or not status:
        raise LedgerError("ledger entry requires amendment_id and status")

    if status == "ungoverned_applied" and governance != "none":
        raise LedgerError("ungoverned_applied is only legal in a governance:none arm (G7.5)")

    entries = read_ledger(org_dir)
    prior = _last_status(entries, amendment_id)
    if prior in _TERMINAL:
        raise LedgerError(f"amendment {amendment_id} is terminal ({prior}); no further transitions")
    allowed = _TRANSITIONS.get(prior, set())
    if status not in allowed:
        raise LedgerError(f"illegal transition {prior!r} -> {status!r} for {amendment_id}")

    record = dict(entry)
    record.setdefault("ts", datetime.now(UTC).isoformat())
    path = ledger_path(org_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def check_asset_loadable(path: Path | str) -> None:
    """Post-apply sanity: the asset must still parse in its native format."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in _LOADABLE_SUFFIXES:
        raise ApplyError(f"unsupported asset suffix: {suffix or '<none>'}")
    text = path.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        yaml.safe_load(text)
    elif suffix == ".json":
        json.loads(text)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified_diff(original: str, diff: str) -> str:
    """Strict unified-diff application: any context mismatch raises ApplyError.

    No fuzzy matching by design — an amendment diff is an institutional
    document; tolerance here would be room for fabrication. Output is
    normalized to end with a single trailing newline when non-empty.
    """
    orig = original.splitlines()
    out: list[str] = []
    pos = 0
    applied_any = False
    lines = diff.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = _HUNK_RE.match(line)
        if match is None:
            if line.startswith(("--- ", "+++ ", "diff ", "index ")) or not line.strip():
                idx += 1
                continue
            raise ApplyError(f"unexpected line outside hunk: {line!r}")
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        hunk_pos = old_start - 1 if old_count > 0 else old_start
        if hunk_pos < pos or hunk_pos > len(orig):
            raise ApplyError("hunk position out of range or overlapping")
        out.extend(orig[pos:hunk_pos])
        pos = hunk_pos
        idx += 1
        consumed = 0
        while idx < len(lines) and _HUNK_RE.match(lines[idx]) is None:
            body = lines[idx]
            if body.startswith("\\"):  # "\ No newline at end of file"
                idx += 1
                continue
            if body.startswith("+"):
                out.append(body[1:])
            elif body.startswith("-") or body.startswith(" ") or body == "":
                expected = body[1:] if body else ""
                if pos >= len(orig) or orig[pos] != expected:
                    got = orig[pos] if pos < len(orig) else "<eof>"
                    raise ApplyError(
                        f"context mismatch at line {pos + 1}: expected {expected!r}, found {got!r}"
                    )
                if not body.startswith("-"):
                    out.append(orig[pos])
                pos += 1
                consumed += 1
            elif body.startswith(("--- ", "+++ ")):
                break
            else:
                raise ApplyError(f"malformed hunk line: {body!r}")
            idx += 1
        if consumed != old_count:
            raise ApplyError(
                f"hunk old-count mismatch: header says {old_count}, body consumed {consumed}"
            )
        applied_any = True
    if not applied_any:
        raise ApplyError("diff contains no hunks")
    out.extend(orig[pos:])
    return "\n".join(out) + ("\n" if out else "")


@dataclass
class ApplyRecord:
    amendment_id: str
    target_asset: str
    change_kind: str
    before_sha256: str | None
    after_sha256: str | None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _archive_path(archive_dir: Path | str, amendment_id: str) -> Path:
    return Path(archive_dir) / f"{amendment_id}.json"


def _write_archive(
    archive_dir: Path | str,
    *,
    amendment_id: str,
    target_asset: str,
    change_kind: str,
    before_text: str | None,
    after_text: str | None,
) -> None:
    """Persist full before/after content for mechanical rollback.

    Ledger entries only ever stored sha256 digests (enough to detect drift,
    not enough to reconstruct anything). Rollback needs the actual bytes, so
    this is a separate, append-once-per-amendment archive keyed by
    amendment_id — one file per amendment, never overwritten after the fact.
    """
    path = _archive_path(archive_dir, amendment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "amendment_id": amendment_id,
                "target_asset": target_asset,
                "change_kind": change_kind,
                "before_text": before_text,
                "after_text": after_text,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def apply_amendment(
    assets_root: Path | str,
    proposal: dict[str, Any],
    *,
    archive_dir: Path | str | None = None,
) -> ApplyRecord:
    """Apply a preflighted proposal to the org asset copy. Defense in depth:
    layer is re-derived here; G/off-whitelist targets raise even if callers
    skipped preflight.

    ``archive_dir``, when given, additionally snapshots full before/after
    text so ``rollback_amendment`` can mechanically reverse this exact
    application later (see that function's docstring). Optional and
    keyword-only so existing callers/tests that only need forward-apply are
    unaffected.
    """
    layer = derive_layer(proposal["target_asset"])
    if layer is None or layer == "G":
        raise ApplyError(f"target not applicable: layer={layer!r}")
    target = Path(assets_root) / proposal["target_asset"]
    kind = proposal["change_kind"]
    diff = proposal["diff"]
    amendment_id = str(proposal.get("amendment_id") or "")

    if kind == "add":
        if target.exists():
            raise ApplyError(f"add target already exists: {proposal['target_asset']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(diff, encoding="utf-8")
        try:
            check_asset_loadable(target)
        except Exception:
            target.unlink()
            raise
        if archive_dir is not None:
            _write_archive(
                archive_dir,
                amendment_id=amendment_id,
                target_asset=proposal["target_asset"],
                change_kind=kind,
                before_text=None,
                after_text=diff,
            )
        return ApplyRecord(
            proposal["amendment_id"], proposal["target_asset"], kind, None, _sha256_text(diff)
        )

    if not target.exists():
        raise ApplyError(f"target missing: {proposal['target_asset']}")
    original = target.read_text(encoding="utf-8")

    if kind == "remove":
        target.unlink()
        if archive_dir is not None:
            _write_archive(
                archive_dir,
                amendment_id=amendment_id,
                target_asset=proposal["target_asset"],
                change_kind=kind,
                before_text=original,
                after_text=None,
            )
        return ApplyRecord(
            proposal["amendment_id"], proposal["target_asset"], kind, _sha256_text(original), None
        )

    if kind == "modify":
        updated = _apply_unified_diff(original, diff)
        target.write_text(updated, encoding="utf-8")
        try:
            check_asset_loadable(target)
        except Exception:
            target.write_text(original, encoding="utf-8")
            raise
        if archive_dir is not None:
            _write_archive(
                archive_dir,
                amendment_id=amendment_id,
                target_asset=proposal["target_asset"],
                change_kind=kind,
                before_text=original,
                after_text=updated,
            )
        return ApplyRecord(
            proposal["amendment_id"],
            proposal["target_asset"],
            kind,
            _sha256_text(original),
            _sha256_text(updated),
        )

    raise ApplyError(f"unknown change_kind: {kind}")


def rollback_amendment(
    org_dir: Path | str,
    archive_dir: Path | str,
    amendment_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Mechanically reverse a promoted amendment and record ``rolled_back``.

    Fail-closed on every ambiguity — this is an institutional asset, not a
    text buffer, so "probably fine" is not an acceptable outcome:

    - the ledger's last status for ``amendment_id`` must be exactly
      ``promoted`` (the G7 state machine already only allows
      ``promoted -> rolled_back``; anything else raises ``LedgerError``);
    - the archive entry written by ``apply_amendment`` at promotion time
      must exist (no archive = no mechanical rollback path; the amendment
      must instead be reversed by drafting a fresh corrective amendment);
    - the asset's current content must still match the archived
      ``after_text`` exactly — if a later amendment touched the same file,
      mechanically restoring an older snapshot would silently discard that
      later change, so this refuses and raises ``AmendmentError`` instead.
    """
    org_dir = Path(org_dir)
    entries = read_ledger(org_dir)
    prior = _last_status(entries, amendment_id)
    if prior != "promoted":
        raise LedgerError(
            f"amendment {amendment_id} is not in a rollback-eligible state (last status: {prior!r})"
        )

    archive_file = _archive_path(archive_dir, amendment_id)
    if not archive_file.is_file():
        raise AmendmentError(
            f"no rollback archive for {amendment_id}; cannot mechanically reverse "
            "(promote must be called with archive_dir=... to enable rollback)"
        )
    archive = json.loads(archive_file.read_text(encoding="utf-8"))
    target_asset = str(archive["target_asset"])
    change_kind = str(archive["change_kind"])
    before_text = archive.get("before_text")
    after_text = archive.get("after_text")
    target = Path(org_dir) / "assets" / target_asset

    current = target.read_text(encoding="utf-8") if target.is_file() else None
    if current != after_text:
        raise AmendmentError(
            f"{target_asset} content has drifted since {amendment_id} was promoted; "
            "refusing mechanical rollback (draft a corrective amendment instead)"
        )

    if change_kind == "add":
        target.unlink()
    elif change_kind == "remove":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(before_text), encoding="utf-8")
    elif change_kind == "modify":
        target.write_text(str(before_text), encoding="utf-8")
    else:  # pragma: no cover — archive is engine-written, defensive only
        raise AmendmentError(f"unknown archived change_kind: {change_kind!r}")

    version = institution_version(Path(org_dir) / "assets")
    entry = append_ledger(
        org_dir,
        {
            "amendment_id": amendment_id,
            "status": "rolled_back",
            "target_asset": target_asset,
            "reason": reason or "mechanical_rollback",
            "institution_version": version,
        },
    )
    return entry


def institution_version(assets_root: Path | str) -> str:
    """Content hash of the whole asset tree — the organization's genome id."""
    root = Path(assets_root)
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "iv_" + digest.hexdigest()[:16]


def audit_alignment(proposal: dict[str, Any], lab_root: Path | str) -> list[str]:
    """G7.4 motivation audit: every failure_ref must resolve to a real artifact
    under lab_root. Prediction-vs-shadow-ledger alignment is judged post-shadow
    by the shadow evaluator (M-E3), not here."""
    violations: list[str] = []
    root = Path(lab_root)
    for ref in proposal.get("motivation", {}).get("failure_refs", []):
        pure = PurePosixPath(ref)
        if pure.is_absolute() or ".." in pure.parts:
            violations.append(f"unsafe_ref:{ref}")
            continue
        if not (root / ref).exists():
            violations.append(f"unresolvable_ref:{ref}")
    return violations
