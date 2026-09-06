"""Exit-artifact helpers: sanitize + promote drafts without hardcoding seats."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from sciteam.llm_worker import sanitize_artifact_payload

_DEFAULT_DRAFT_GLOBS = ("*_draft.json", "*_draft*.json")


def draft_candidates(
    mission_dir: Path | str,
    exit_contract: str,
    *,
    globs: list[str] | tuple[str, ...] | None = None,
) -> list[Path]:
    """Draft files under mission_dir matching configured globs (agent-authored)."""
    root = Path(mission_dir)
    contract = str(exit_contract or "").strip()
    patterns = list(globs) if globs else list(_DEFAULT_DRAFT_GLOBS)
    # Always consider the conventional `<contract>_draft.json` first when present.
    ordered_patterns = [f"{contract}_draft.json", *patterns] if contract else patterns
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in ordered_patterns:
        p = str(pattern or "").strip()
        if not p:
            continue
        for hit in sorted(root.glob(p)):
            if not hit.is_file():
                continue
            rp = hit.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            found.append(hit)
    return found


def promote_draft_to_exit(
    *,
    mission_dir: Path | str,
    exit_contract: str,
    artifact_path: Path | str | None = None,
    validator: Any | None = None,
    prefer: Path | str | None = None,
    globs: list[str] | tuple[str, ...] | None = None,
) -> Path:
    """Sanitize an existing agent draft and write the canonical exit file.

    Raises FileNotFoundError if no draft exists, ValueError if none validate.
    """
    root = Path(mission_dir)
    dest = Path(artifact_path) if artifact_path else root / f"{exit_contract}.json"
    if dest.is_file():
        return dest

    candidates: list[Path] = []
    if prefer is not None:
        p = Path(prefer)
        if p.is_file():
            candidates.append(p)
    candidates.extend(draft_candidates(root, exit_contract, globs=globs))
    seen: set[Path] = set()
    ordered: list[Path] = []
    for c in candidates:
        rp = c.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        ordered.append(c)

    if not ordered:
        raise FileNotFoundError(f"no agent draft found under {root} for contract {exit_contract}")

    errors: list[str] = []
    for src in ordered:
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{src.name}: {exc}")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{src.name}: not a JSON object")
            continue
        payload = sanitize_artifact_payload(raw)
        if not isinstance(payload, dict):
            errors.append(f"{src.name}: sanitize produced non-object")
            continue
        if validator is not None:
            tmp = root / f".promote_check_{exit_contract}.json"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                if hasattr(validator, "validate_file"):
                    errs = validator.validate_file(exit_contract, tmp)
                elif hasattr(validator, "validate_data"):
                    errs = validator.validate_data(exit_contract, payload)
                else:
                    errs = []
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
            if errs:
                errors.append(f"{src.name}: " + "; ".join(str(e) for e in errs))
                continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return dest

    raise ValueError("no draft passed sanitize/schema; tried: " + "; ".join(errors)[:2000])
