"""Pure helpers shared by every W3-harness arm (own-worker, Pi) that plugs
into ``RuntimePort``.

Institution-repair B2 rationale: role-duty gating (who may emit the exit
artifact, who is a round-assessor, which tool capabilities a skills
allowlist requires) is a *contract-level* concern, not a worker-implementation
concern. Before this module existed it lived only inside
``LlmWorkerRuntime`` as private methods; duplicating it into ``PiRuntime``
would have recreated exactly the kind of "two parallel copies silently
drift" risk A3/A4 fixed for role/skill registries. Single source of truth
here; both runtimes delegate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "role_key",
    "extract_json",
    "strip_nulls",
    "sanitize_artifact_payload",
    "artifact_duty_tokens",
    "is_artifact_role",
    "assess_role_tokens",
    "is_assess_role",
    "contract_schema_text",
    "required_tool_capabilities",
    "effective_tool_capabilities",
    "commit_memory_record",
    "FS_ALWAYS_CAPABILITIES",
    "BASH_GATING_CAPABILITIES",
]

# B4: file-workspace tools are granted unconditionally, every seat, every
# arm — matching the pi arm's always-on native read/write/edit/grep/find/ls
# (see `pi_runtime._ALWAYS_TOOLS`). Before this, `LlmWorkerRuntime` had no
# mid-round read/search surface at all (only a round-end `envelope["files"]`
# write), which is what made the `build_fix_loop` seat web-search for a
# local problem spec it could not `read` (HARNESS_SOTA_AUDIT.md).
FS_ALWAYS_CAPABILITIES = frozenset(
    {"fs.read", "fs.write", "fs.edit", "fs.grep", "fs.find", "fs.list"}
)

# Capabilities whose presence already justifies a process-level bash escape
# hatch (compute/build work, or literature/web access that already implies
# outbound network use). Single source of truth for both arms' bash gate —
# `PiRuntime.tools_for_capabilities` and the own arm's tool surface both
# call `effective_tool_capabilities` below, so the two arms cannot silently
# drift on which seats get shell access (the A3/A4 lesson: two parallel
# copies of a gating rule rot independently).
BASH_GATING_CAPABILITIES = frozenset(
    {
        "compute.sandbox",
        "compute.eval",
        "compute.submit",
        "literature.search",
        "literature.audit",
        "web.search",
        "web.fetch",
        "model.certify",
        "dataset.materialize",
    }
)


def role_key(agent_key: str) -> str:
    """``debater_2`` -> ``debater``; ``literature_lead__r1_abc`` -> ``literature_lead``."""
    match = re.fullmatch(r"(.+)_(\d+)", agent_key)
    return match.group(1) if match else agent_key


def extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def strip_nulls(value: Any) -> Any:
    """Drop null-valued keys recursively: models emit `"optional": null` for
    schema-optional fields, which draft-2020 schemas reject."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    return value


def sanitize_artifact_payload(value: Any) -> Any:
    """Prepare model JSON for schema validation.

    - Drop null optional fields (draft-2020 rejects `null` for object types)
    - Drop private `_`-prefixed keys (e.g. `_meta_review_notes`) that models
      add as side-channel notes but schemas forbid via additionalProperties
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            if child is None or str(key).startswith("_"):
                continue
            out[str(key)] = sanitize_artifact_payload(child)
        return out
    if isinstance(value, list):
        return [sanitize_artifact_payload(v) for v in value if v is not None]
    return value


def artifact_duty_tokens(meta: dict[str, Any] | None) -> set[str]:
    declared = (meta or {}).get("artifact_emit_roles") or []
    if not isinstance(declared, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in declared if str(x).strip()}


def assess_role_tokens(meta: dict[str, Any] | None) -> set[str]:
    declared = (meta or {}).get("assess_roles") or []
    if not isinstance(declared, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in declared if str(x).strip()}


def is_assess_role(agent_key: str, meta: dict[str, Any] | None = None) -> bool:
    tokens = assess_role_tokens(meta)
    if not tokens:
        return False
    role = role_key(agent_key)
    return role in tokens or agent_key in tokens


def is_artifact_role(agent_key: str, meta: dict[str, Any] | None = None) -> bool:
    """True only if paradigm metadata lists this seat as exit-artifact duty."""
    if is_assess_role(agent_key, meta):
        return False
    tokens = artifact_duty_tokens(meta)
    if not tokens:
        return False
    role = role_key(agent_key)
    return role in tokens or agent_key in tokens


def contract_schema_text(schemas_dir: Path | str, contract: str) -> str:
    name = contract if contract.endswith(".schema.json") else f"{contract}.schema.json"
    path = Path(schemas_dir) / name
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return "(schema unavailable)"


def required_tool_capabilities(skills_dir: Path | str, skills_allowlist: list[str]) -> set[str]:
    from sciteam.capabilities import collect_required

    required_map = collect_required(skills_dir, skills_allowlist)
    required: set[str] = set()
    for caps in required_map.values():
        required.update(caps)
    return required


def effective_tool_capabilities(required_caps: set[str] | frozenset[str]) -> set[str]:
    """Skill-declared capabilities, plus the base file-workspace tools every
    seat always gets, plus `compute.bash` when the declared set already
    justifies process-level access. See `FS_ALWAYS_CAPABILITIES` /
    `BASH_GATING_CAPABILITIES` above for the rationale; this is the single
    function both `RuntimePort` arms call to build their tool surface from
    a seat's raw declared capabilities."""
    effective = set(required_caps) | FS_ALWAYS_CAPABILITIES
    if set(required_caps) & BASH_GATING_CAPABILITIES:
        effective.add("compute.bash")
    return effective


def commit_memory_record(
    *, work_dir: str, agent_key: str, meta: dict[str, Any], record: Any
) -> None:
    """Commit to the seat's own history, and — when the mission was created
    with a ``campaign_memory_path`` pointer — to this role's cross-mission
    memory too (P1-5). Shared by every ``RuntimePort`` arm so W4's memory
    continuity does not depend on which harness executed the seat."""
    from sciteam.memory import CampaignMemoryStore, SeatMemoryStore

    SeatMemoryStore(work_dir).commit(record)
    campaign_memory_path = str(meta.get("campaign_memory_path") or "")
    if campaign_memory_path:
        CampaignMemoryStore(campaign_memory_path).commit(role_key(agent_key), record)
