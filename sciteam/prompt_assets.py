"""Prompt assembly for the reference LLM worker.

Two things live here, both pure data/lookup with no runtime state:

* the model-facing envelope instructions — the JSON contract a seat must
  answer in, and the capability-gated clauses appended to it;
* :class:`PromptAssets`, the filesystem-backed lookup for the constitution,
  role prompts and skill bodies (including the plugin-registered extra
  search directories).

Kept out of ``llm_worker.py`` so that changing what a seat is *told* is a
diff in one place, separate from how the tool loop and contract repair
*behave*. ``llm_worker`` re-exports these names, so existing imports keep
working.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sciteam.worker_common import role_key as _role_key

# P1-8: full-allowlist skill injection (every id, each capped at
# `max_skill_chars`) doesn't scale — once a role's allowlist grows past this
# many skills, only the top-`_SKILL_TOPK_K` by deterministic lexical overlap
# with the task card get their body text injected into the prompt. This is
# strictly a *prompt-injection* selection: `_required_tool_capabilities`
# still derives from the FULL allowlist (see `run_subagent`), so which
# capabilities/tools a seat is granted never shrinks because a skill's text
# wasn't shown this round (capability surface unchanged, per audit).
_SKILL_TOPK_THRESHOLD = 6
_SKILL_TOPK_K = 6

ENVELOPE_INSTRUCTIONS = """\
You may call research tools (literature_search, web_search, url_fetch, citation_audit)
before finishing. When the mission needs literature or external facts, you MUST
use tools — do not invent DOIs, titles, or metrics.

You also have workspace-confined file tools, always available: read, write, edit,
ls, find, grep. Use `read`/`grep`/`find`/`ls` to inspect existing files (a problem
spec, another seat's output, prior code) before acting — never assume or invent
their contents. Use `write`/`edit` for incremental work you want on disk *now*
(these take effect immediately, unlike the round-end "files" key below).
For source code or any long content, you MUST use `write`/`edit`. Do not place
source code in the JSON `files` value; after writing it with a tool, emit
`"files": null`.

When finished, respond with a single JSON object (no markdown fences, no commentary outside JSON):
{
  "summary": "<what you concluded / produced, concise but complete>",
  "facts": ["<atomic reusable finding>", ...],
  "artifact": <the exit-contract JSON object if YOU are finalizing it this round, else null>,
  "files": {"<relative filename>": "<full file content>", ...} | null,
  "run_eval": {"problem_id": "<registered id>", "candidate_file": "<file you wrote>"} | null,
  "tool_notes": ["<optional: which tools you used and why>"] | null
}
Only emit "artifact" when your role is responsible for assembling the final
mission artifact and the content is ready; partial work belongs in summary/facts.
Inside the artifact, OMIT optional fields you cannot fill — never emit null
for them, and never add fields the schema does not declare.
"files" is only for a short text note that cannot be written through the
workspace tools. Candidate code and manuscripts must be written with
`write`/`edit`. "run_eval" invokes the FROZEN eval script on a candidate file
you wrote; its results.json is written to the mission artifact path
automatically — this is the ONLY way official numbers come to exist. Use these
only when the mission calls for them; otherwise emit null.
"""

ASSESS_ENVELOPE_INSTRUCTIONS = """\
You are a round-assessment seat (may_assess_round). You do NOT write the mission
exit-contract file. Apply the charter to the observation pack and decide.

When finished, respond with a single JSON object (no markdown fences):
{
  "decision": "continue" | "completed" | "failed",
  "reason_code": "ok_complete" | "need_artifact" | "gate_unsatisfied" | "exit_stalled" |
    "integrity_fail" | "process_fail" | "all_workers_failed",
  "summary": "<short rationale>",
  "next_round_hint": "<guidance for production seats, or empty string>",
  "assessor_key": "<your seat key>",
  "approve_promote_draft": false,
  "facts": []
}
"artifact" must be null. Do not invent science content.
"""

# B4: appended to whichever envelope-instructions block is active, only for
# seats whose declared capabilities already justify process-level access
# (`worker_common.BASH_GATING_CAPABILITIES`) — mirrors the pi arm's
# capability-gated `bash` (see `pi_runtime._PI_ARTIFACT_INSTRUCTIONS`'s
# `bash_clause`), so a skill allowlist grants the same shell access on
# either arm.
_BASH_TOOL_CLAUSE = """
You also have `bash`: a shell command cwd-confined to the mission workspace,
bounded in time/CPU/memory with truncated output. Use it for build/test/compute
steps your other tools cannot do (running a build, invoking a CLI, inspecting an
environment) — never to fetch URLs when literature_search/web_search/url_fetch
already cover that, and never to work around the workspace confinement.
"""


@dataclass
class PromptAssets:
    """Filesystem-backed prompt/skill lookup.

    When ``pack_dir`` is set (experiment instance root), role prompts and skills
    are loaded from the pack first; system ``assets/`` remain templates.

    ``genome_overlay_dir`` (Line D, lineD/03 §11): when set, every
    ``prompt_modules/*.md`` under it is appended to each role prompt as an
    evolved-guidance section. ``None`` (the default) is byte-identical to the
    pre-genome behavior — the Line A arms never pass it.

    ``extra_prompts_dirs``/``extra_skills_dirs`` (see
    ``docs/PLUGIN-GUIDE.md``,
    ``PluginRegistry.role_prompts_dirs``/``skills_dirs``): additional search
    directories consulted, in order, after ``prompts_dir``/``skills_dir`` and
    ``pack_dir`` find no match. Empty tuples (the default) reproduce the
    pre-plugin lookup order byte-for-byte — a plugin can *add* a role prompt
    or skill body for an id the base assets don't define, never shadow one
    that already resolves from ``prompts_dir``/``skills_dir``/``pack_dir``.
    """

    prompts_dir: Path
    skills_dir: Path
    max_skill_chars: int = 16000
    pack_dir: Path | None = None
    genome_overlay_dir: Path | None = None
    extra_prompts_dirs: tuple[Path, ...] = ()
    extra_skills_dirs: tuple[Path, ...] = ()
    loaded_skill_versions: dict[str, dict[str, str]] = field(default_factory=dict, init=False)

    def constitution(self) -> str:
        path = self.prompts_dir / "constitution.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def role_prompt(self, agent_key: str) -> str:
        return self._base_role_prompt(agent_key) + self._genome_modules()

    def _base_role_prompt(self, agent_key: str) -> str:
        role = _role_key(agent_key)
        if self.pack_dir is not None:
            pack_agent = Path(self.pack_dir) / "agents" / role / "AGENT.md"
            if pack_agent.is_file():
                text = pack_agent.read_text(encoding="utf-8")
                # Strip YAML frontmatter for the model-facing prompt body.
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        text = parts[2].lstrip("\n")
                return text
        search_dirs = (self.prompts_dir, *self.extra_prompts_dirs)
        # Pass 1: a role-specific file, in any search dir — a plugin's
        # specific role must be found before falling through to the base's
        # generic `worker.md` catch-all (pass 2), or a plugin-added role
        # would never be reachable (the base ships a `worker.md`, so it
        # would always win pass 1 of a single combined per-dir loop).
        for prompts_dir in search_dirs:
            path = Path(prompts_dir) / "roles" / f"{role}.md"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        # Pass 2: the legacy generic fallback, same search order.
        for prompts_dir in search_dirs:
            path = Path(prompts_dir) / "roles" / "worker.md"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return f"You are the `{role}` specialist on a research team."

    def _genome_modules(self) -> str:
        if self.genome_overlay_dir is None:
            return ""
        mod_dir = Path(self.genome_overlay_dir) / "prompt_modules"
        if not mod_dir.is_dir():
            return ""
        parts = [
            body
            for path in sorted(mod_dir.glob("*.md"))
            if (body := path.read_text(encoding="utf-8").strip())
        ]
        if not parts:
            return ""
        return "\n\n## Evolved guidance (genome overlay)\n\n" + "\n\n".join(parts)

    def skill_raw_text(self, skill_id: str) -> tuple[str, str]:
        """`(text, source_path)` for one skill id, pack override first, then
        the system `skills_dir` — the same lookup `skill_pack` uses per
        entry, factored out so P1-8 top-k scoring can read skill bodies
        without duplicating (and risking drifting from) this lookup order."""
        if self.pack_dir is not None:
            pack_skill = Path(self.pack_dir) / "skills" / skill_id / "SKILL.md"
            if pack_skill.is_file():
                return pack_skill.read_text(encoding="utf-8"), str(pack_skill)
        for skills_dir in (self.skills_dir, *self.extra_skills_dirs):
            path = Path(skills_dir) / skill_id / "SKILL.md"
            if path.is_file():
                return path.read_text(encoding="utf-8"), str(path)
        return "", ""

    def skill_pack(self, allowlist: list[str], *, max_chars_override: int | None = None) -> str:
        max_chars = self.max_skill_chars if max_chars_override is None else max_chars_override
        sections: list[str] = []
        for skill_id in allowlist:
            text, source = self.skill_raw_text(skill_id)
            if text:
                version_match = re.search(r"(?m)^(?:version:\s*|##\s+Version\s*\n)([^\n]+)", text)
                self.loaded_skill_versions[skill_id] = {
                    "version": (
                        version_match.group(1).strip().strip("\"'")
                        if version_match
                        else "unversioned"
                    ),
                    "source": source,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n...[truncated]"
                sections.append(f'<skill id="{skill_id}">\n{text}\n</skill>')
            else:
                sections.append(
                    f'<skill id="{skill_id}">(spec not found — follow the id '
                    "semantics conservatively)</skill>"
                )
        return "\n\n".join(sections)
