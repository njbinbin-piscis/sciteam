"""Skill-declared capabilities and mission preflight.

Thesis-aligned gate (generic, not stage-specific):

1. Skills declare the capabilities they need.
2. Runtime advertises what it can actually provide.
3. Mission start fails closed when a required capability is missing —
   agents must not improvise with training memory as evidence.

Capability ids match ``assets/tools/catalog.yaml`` ``capability`` fields.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Capabilities backed by the campaign/wizard research tool loop.
RESEARCH_TOOL_CAPABILITIES = frozenset(
    {
        "literature.search",
        "literature.audit",
        "web.search",
        "web.fetch",
        "compute.sandbox",
        "model.certify",
    }
)

# Catalog capability → whether lab code can currently provide it.
# ``dataset.materialize`` stays False until an adapter is wired.
_LAB_BASE_AVAILABLE = frozenset(
    {
        "literature.search",
        "literature.audit",
        "web.search",
        "web.fetch",
        "compute.sandbox",
        "model.certify",
        # Grundnorm G7 mechanical amendment audit (wizard_tools.amendment_audit);
        # required by org.amend_review seats in amendment_review missions.
        "institution.audit",
    }
)


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What this process can honestly claim to provide."""

    available: frozenset[str]

    def has(self, capability: str) -> bool:
        return capability in self.available

    @classmethod
    def lab(
        cls,
        *,
        eval_runner: bool = True,
        dataset_materialize: bool = False,
        extra: Iterable[str] = (),
    ) -> RuntimeCapabilities:
        caps = set(_LAB_BASE_AVAILABLE)
        if eval_runner:
            caps.add("compute.eval")
            # submit is reached only via injected eval/compute ports, not as a
            # free agent action — still advertise when an eval runner exists.
            caps.add("compute.submit")
        if dataset_materialize:
            caps.add("dataset.materialize")
        caps.update(str(x) for x in extra)
        return cls(frozenset(caps))


@dataclass(frozen=True)
class PreflightIssue:
    skill_id: str
    capability: str
    detail: str

    def format(self) -> str:
        return f"{self.skill_id} requires `{self.capability}` ({self.detail})"


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    issues: tuple[PreflightIssue, ...]
    required: frozenset[str]

    def summary(self) -> str:
        if self.ok:
            return "capability preflight ok"
        lines = "; ".join(i.format() for i in self.issues)
        return f"capability preflight failed: {lines}"


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_REQUIRES_SECTION_RE = re.compile(
    r"(?im)^##\s+Requires\s+capabilities\s*\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.DOTALL,
)


def parse_requires_capabilities(skill_md: str) -> tuple[str, ...]:
    """Extract capability ids from YAML frontmatter and/or a markdown section.

    Accepted forms::

        ---
        requires_capabilities:
          - literature.search
        ---

        ## Requires capabilities
        - literature.search
        - literature.audit
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        cap = raw.strip().strip("`").strip('"').strip("'")
        if not cap or cap.startswith("#") or cap in seen:
            return
        seen.add(cap)
        found.append(cap)

    fm = _FRONTMATTER_RE.match(skill_md)
    if fm:
        block = fm.group(1)
        # Minimal YAML subset: key then list items, or inline [a, b]
        if re.search(r"(?m)^requires_capabilities\s*:", block):
            inline = re.search(
                r"(?m)^requires_capabilities\s*:\s*\[([^\]]*)\]\s*$",
                block,
            )
            if inline:
                for part in inline.group(1).split(","):
                    _add(part)
            else:
                in_list = False
                for line in block.splitlines():
                    if re.match(r"^requires_capabilities\s*:", line):
                        in_list = True
                        rest = line.split(":", 1)[1].strip()
                        if rest.startswith("[") and rest.endswith("]"):
                            for part in rest[1:-1].split(","):
                                _add(part)
                            in_list = False
                        continue
                    if in_list:
                        if re.match(r"^[A-Za-z_][\w]*\s*:", line):
                            break
                        m = re.match(r"^\s*-\s+(.+)$", line)
                        if m:
                            _add(m.group(1))

    section = _REQUIRES_SECTION_RE.search(skill_md)
    if section:
        for line in section.group("body").splitlines():
            m = re.match(r"^\s*[-*]\s+(.+)$", line)
            if m:
                _add(m.group(1))
            elif re.match(r"^\s*`[^`]+`\s*$", line):
                _add(line)

    return tuple(found)


def skill_requires(skills_dir: Path | str, skill_id: str) -> tuple[str, ...]:
    path = Path(skills_dir) / skill_id / "SKILL.md"
    if not path.is_file():
        return ()
    return parse_requires_capabilities(path.read_text(encoding="utf-8"))


def collect_required(
    skills_dir: Path | str,
    allowlist: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Map skill_id → required capability tuple for the allowlist."""
    out: dict[str, tuple[str, ...]] = {}
    for skill_id in allowlist:
        sid = str(skill_id).strip()
        if not sid:
            continue
        out[sid] = skill_requires(skills_dir, sid)
    return out


def preflight_mission(
    *,
    skills_dir: Path | str,
    allowlist: Iterable[str],
    available: frozenset[str] | RuntimeCapabilities,
) -> PreflightResult:
    """Fail closed when any allowlisted skill needs a missing capability."""
    caps = available.available if isinstance(available, RuntimeCapabilities) else available
    required_map = collect_required(skills_dir, allowlist)
    issues: list[PreflightIssue] = []
    required: set[str] = set()
    for skill_id, needs in required_map.items():
        for cap in needs:
            required.add(cap)
            if cap not in caps:
                issues.append(
                    PreflightIssue(
                        skill_id=skill_id,
                        capability=cap,
                        detail="not available in this runtime — refuse to start; "
                        "do not substitute training memory for evidence",
                    )
                )
    return PreflightResult(
        ok=not issues,
        issues=tuple(issues),
        required=frozenset(required),
    )


def research_tools_needed(required: Iterable[str]) -> bool:
    """True when the mission's declared caps need the research tool loop."""
    return bool(RESEARCH_TOOL_CAPABILITIES.intersection(required))
