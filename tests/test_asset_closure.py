"""Asset-graph closure: every declared skill must have a materialized body.

DEF-NEW-16 class guard. A skill id that is reachable from any plan, playbook,
role catalog, paradigm, harness seed or INDEX entry — but has no SKILL.md —
is a treatment defect: seats are authorized to use text that does not exist.
Fail closed here, before any campaign starts.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

LAB_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = LAB_ROOT / "assets" / "skills"
INDEX_MD = SKILLS_DIR / "INDEX.md"

SKILL_LIST_KEYS = {"skills_allowlist", "default_skills", "skills"}
# `- skill.id` optionally followed by an em-dash annotation.
INDEX_LINE = re.compile(r"^- ([a-z][a-z0-9_.]+)(?:\s+—.*)?$", re.M)


def _disk_skill_dirs() -> set[str]:
    return {
        d.name
        for d in SKILLS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    }


def _disk_skills_with_body() -> set[str]:
    return {name for name in _disk_skill_dirs() if (SKILLS_DIR / name / "SKILL.md").is_file()}


def _yaml_referenced() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}

    def walk(node: object, src: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in SKILL_LIST_KEYS and isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            refs.setdefault(item, set()).add(src)
                walk(val, src)
        elif isinstance(node, list):
            for item in node:
                walk(item, src)

    assets = LAB_ROOT / "assets"
    for path in list(assets.rglob("*.yaml")) + list(assets.rglob("*.yml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        for doc in docs:
            walk(doc, str(path.relative_to(LAB_ROOT)))
    return refs


def _python_referenced() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}

    def note_list(node: ast.AST, src: str) -> None:
        if isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    refs.setdefault(elt.value, set()).add(src)

    for root in ("harness", "sciteam"):
        for path in (LAB_ROOT / root).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            src = str(path.relative_to(LAB_ROOT))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, val in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in SKILL_LIST_KEYS
                        ):
                            note_list(val, src)
                elif isinstance(node, ast.keyword) and node.arg == "skills_allowlist":
                    note_list(node.value, src)
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and "SKILLS" in target.id:
                            for val in node.value.values:
                                note_list(val, src)
    return refs


def test_every_skill_dir_has_body():
    shells = sorted(_disk_skill_dirs() - _disk_skills_with_body())
    assert not shells, f"empty skill shells (dir without SKILL.md): {shells}"


def test_index_matches_disk_both_ways():
    listed = set(INDEX_LINE.findall(INDEX_MD.read_text(encoding="utf-8")))
    disk = _disk_skill_dirs()
    assert not listed - disk, f"INDEX lists skills missing on disk: {sorted(listed - disk)}"
    assert not disk - listed, f"skills on disk missing from INDEX: {sorted(disk - listed)}"


def test_yaml_referenced_skills_are_materialized():
    ok = _disk_skills_with_body()
    dangling = {
        sid: sorted(srcs)
        for sid, srcs in _yaml_referenced().items()
        if sid not in ok
    }
    assert not dangling, f"yaml-declared skills without body: {dangling}"


def test_python_referenced_skills_are_materialized():
    ok = _disk_skills_with_body()
    dangling = {
        sid: sorted(srcs)
        for sid, srcs in _python_referenced().items()
        if sid not in ok
    }
    assert not dangling, f"python-declared skills without body: {dangling}"
