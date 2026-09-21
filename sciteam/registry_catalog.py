"""Live registries from on-disk institutions (not synonym tables).

Line A constraint: the engine must not hardcode research-stage vocabularies
(HYPOTHESIZE / literature_search / …). Allowed paradigm / exit_contract ids
are whatever currently ships under assets/ + schemas/ — discovered at runtime.
"""

from __future__ import annotations

from functools import lru_cache

from sciteam._paths import ASSETS_ROOT, SCHEMAS_DIR

PARADIGMS_DIR = ASSETS_ROOT / "teams" / "paradigms"


@lru_cache(maxsize=1)
def registered_paradigm_ids() -> frozenset[str]:
    if not PARADIGMS_DIR.is_dir():
        return frozenset()
    return frozenset(p.stem for p in PARADIGMS_DIR.glob("*.yaml"))


@lru_cache(maxsize=1)
def registered_exit_contract_ids() -> frozenset[str]:
    if not SCHEMAS_DIR.is_dir():
        return frozenset()
    out: set[str] = set()
    for p in SCHEMAS_DIR.glob("*.schema.json"):
        stem = p.name[: -len(".schema.json")] if p.name.endswith(".schema.json") else p.stem
        out.add(stem)
    return frozenset(out)


def registry_notes_for_plan(plan: dict | None) -> list[str]:
    """Reference notes for the auditor / wizard — never silently remapped."""
    if not isinstance(plan, dict):
        return []
    paradigms = registered_paradigm_ids()
    contracts = registered_exit_contract_ids()
    notes: list[str] = []
    for i, node in enumerate(plan.get("missions") or []):
        if not isinstance(node, dict):
            continue
        mission = node.get("mission") if isinstance(node.get("mission"), dict) else node
        if not isinstance(mission, dict):
            continue
        mid = str(mission.get("id") or i)
        pid = str(mission.get("paradigm") or "").strip()
        if pid and pid not in paradigms:
            notes.append(
                f"mission «{mid}» paradigm={pid!r} 不在平台范式注册表 "
                f"(assets/teams/paradigms) — use a registered template id, or ship the "
                f"paradigm from a plugin via register_paradigm_dir"
            )
        ec = mission.get("exit_contract")
        if isinstance(ec, dict):
            notes.append(
                f"mission «{mid}» exit_contract 必须是 schemas/ 下的契约 id 字符串，不能是散文/对象"
            )
        else:
            eid = str(ec or "").strip()
            if eid and eid not in contracts:
                notes.append(
                    f"mission «{mid}» exit_contract={eid!r} 不在契约注册表 "
                    f"(schemas/*.schema.json) — use a registered exit-contract id"
                )
    return notes
