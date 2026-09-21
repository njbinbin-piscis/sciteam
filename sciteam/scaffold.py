"""Project scaffolding for SciTeam.

Generates a runnable, testable institutional multi-agent project from any of
the 21 bundled paradigm templates:
* Copies and specializes the paradigm manifest (`institution.yaml`).
* Scaffolds role prompts for every seat declared in the manifest.
* Emits a frozen test suite (`test_institution.py`) checking load invariants
  and mutation survivability.
* Emits a project `README.md` documenting seats, authority, and execution.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sciteam._paths import ASSETS_ROOT
from sciteam.mutation import mutate_file

PARADIGMS_DIR = ASSETS_ROOT / "teams" / "paradigms"
PROMPTS_ROLES_DIR = ASSETS_ROOT / "prompts" / "roles"


@dataclass(frozen=True, slots=True)
class TemplateInfo:
    id: str
    name: str
    description: str
    topology: str
    members_count: int
    roles: tuple[str, ...]


def list_templates() -> list[TemplateInfo]:
    """List all available paradigm templates."""
    results: list[TemplateInfo] = []
    if not PARADIGMS_DIR.is_dir():
        return results

    for path in sorted(PARADIGMS_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            members = raw.get("members") or []
            roles = tuple(
                str(m.get("role") or m.get("key")) for m in members if isinstance(m, dict)
            )
            coordination = raw.get("coordination") or {}
            topology = str(coordination.get("topology") or "custom")
            desc = str(raw.get("description") or "").strip().split("\n")[0]
            results.append(
                TemplateInfo(
                    id=str(raw.get("id") or path.stem),
                    name=str(raw.get("name") or path.stem),
                    description=desc,
                    topology=topology,
                    members_count=len(members),
                    roles=roles,
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return results


def find_template(template_id: str) -> Path | None:
    """Resolve a template ID to its YAML manifest path."""
    direct = PARADIGMS_DIR / f"{template_id}.yaml"
    if direct.is_file():
        return direct
    direct_exact = PARADIGMS_DIR / template_id
    if direct_exact.is_file():
        return direct_exact
    for p in PARADIGMS_DIR.glob("*.yaml"):
        if p.stem == template_id:
            return p
    return None


def scaffold_project(
    template_id: str,
    target_dir: Path | str,
    *,
    project_name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Scaffold a new institutional project from a template.

    Returns a summary dict with generated paths and metadata.
    """
    tmpl_path = find_template(template_id)
    if tmpl_path is None:
        raise ValueError(
            f"Unknown template '{template_id}'. Run 'sciteam init --list' to see options."
        )

    target = Path(target_dir).resolve()
    if target.exists() and not overwrite and any(target.iterdir()):
        raise FileExistsError(
            f"Target directory '{target}' is not empty. Use --overwrite or pick a new directory."
        )
    target.mkdir(parents=True, exist_ok=True)

    raw_manifest = yaml.safe_load(tmpl_path.read_text(encoding="utf-8"))
    if not isinstance(raw_manifest, dict):
        raise ValueError(f"Template '{template_id}' is not a valid YAML mapping")

    name = project_name or str(raw_manifest.get("name") or target.name)
    raw_manifest["name"] = name
    raw_manifest["id"] = target.name.lower().replace("-", "_").replace(" ", "_")

    manifest_dest = target / "institution.yaml"
    dumped = yaml.dump(raw_manifest, sort_keys=False, allow_unicode=True)
    manifest_dest.write_text(dumped, encoding="utf-8")

    # Scaffolding role prompts
    prompts_dir = target / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    created_prompts: list[str] = []

    members = raw_manifest.get("members") or []
    for member in members:
        if not isinstance(member, dict):
            continue
        role = str(member.get("role") or member.get("key") or "worker")
        role_file = prompts_dir / f"{role}.md"
        if not role_file.exists():
            source_role = PROMPTS_ROLES_DIR / f"{role}.md"
            if source_role.is_file():
                shutil.copy2(source_role, role_file)
            else:
                authorities = member.get("authority") or []
                auth_desc = ", ".join(authorities) if authorities else "standard duties"
                content = (
                    f"# Role: {member.get('name', role.title())}\n\n"
                    f"You occupy seat `{member.get('key', role)}` in institution `{name}`.\n\n"
                    f"## Institutional Authority\n"
                    f"Your seat is granted authority: {auth_desc}.\n\n"
                    "## Operational Norms\n"
                    "1. Strictly observe your institutional boundaries.\n"
                    "2. Return decisions in the required JSON envelope.\n"
                )
                role_file.write_text(content, encoding="utf-8")
            created_prompts.append(str(role_file.relative_to(target)))

    # Emit test_institution.py
    test_file = target / "test_institution.py"
    test_content = (
        '"""Institutional invariant and mutation tests for this project."""\n\n'
        "from pathlib import Path\n"
        "from sciteam.team_loader import load_team_config\n"
        "from sciteam.mutation import mutate_file\n\n"
        "MANIFEST_PATH = Path(__file__).parent / 'institution.yaml'\n\n\n"
        "def test_institution_manifest_loads_cleanly():\n"
        "    config = load_team_config(MANIFEST_PATH)\n"
        "    assert config is not None\n"
        "    assert len(config.members) >= 1\n\n\n"
        "def test_institution_manifest_mutation_score():\n"
        "    report = mutate_file(MANIFEST_PATH)\n"
        "    assert report.ok, f'Mutation testing failed: {report.survivors}'\n"
        "    assert report.score == 1.0\n"
    )
    test_file.write_text(test_content, encoding="utf-8")

    # Emit README.md
    readme_file = target / "README.md"
    members_lines = []
    for m in members:
        if isinstance(m, dict):
            k = m.get("key", "")
            r = m.get("role", "")
            auth = ", ".join(m.get("authority") or []) or "(none)"
            emitter = " [exit emitter]" if m.get("emits_exit_artifact") else ""
            members_lines.append(f"- **`{k}`** (role: `{r}`{emitter}): authority `[{auth}]`")
    members_block = "\n".join(members_lines)

    readme_content = (
        f"# {name}\n\n"
        f"Institutional Multi-Agent System generated from template `{template_id}`.\n\n"
        f"## Roster and Authority\n\n"
        f"{members_block}\n\n"
        "## Validation & Mutation Testing\n\n"
        "Verify institutional invariants and rule strictness:\n\n"
        "```bash\n"
        "sciteam validate institution.yaml\n"
        "sciteam mutate institution.yaml\n"
        "pytest test_institution.py\n"
        "```\n\n"
        "## Execution\n\n"
        "Run with your configured `RuntimePort` backend (e.g. OpenAI, DeepSeek, or local models).\n"
    )
    readme_file.write_text(readme_content, encoding="utf-8")

    # Verify that generated project passes validation and mutation tests
    verify_report = mutate_file(manifest_dest)

    return {
        "template": template_id,
        "target_dir": str(target),
        "manifest": str(manifest_dest),
        "prompts": created_prompts,
        "test_file": str(test_file),
        "readme": str(readme_file),
        "mutation_score": verify_report.score,
        "mutation_ok": verify_report.ok,
    }
