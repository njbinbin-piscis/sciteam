from pathlib import Path

from sciteam.harness.config import HarnessConfig
from sciteam.harness.state import ActivationState
from sciteam.kernel.config_store import ConfigStore, split_front_matter
from sciteam.providers.llm.types import ChatMessage, ToolSpec

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier work in this activation was compacted to save context. The "
    "summary below replaces those messages; the task statement above is unchanged and still "
    "applies. Continue from where the summary leaves off.\n\n"
)


def skill_index(configs: ConfigStore | Path | None = None, root: str | None = None) -> str:
    """Deterministically build a compact (<200 chars/entry) index of `skills/<name>/SKILL.md`
    frontmatter without expanding the skill body. `root`: where the agent reads the configs
    directory (paths are shown under it). Returns `""` when no skills exist."""
    if configs is None:
        return ""
    store = configs if isinstance(configs, ConfigStore) else ConfigStore(Path(configs))
    entries: list[str] = []
    for sname in store.list_dir("skills"):
        rel = f"skills/{sname}/SKILL.md"
        try:
            raw = store.load_text(rel)
        except FileNotFoundError:
            continue
        meta, _body = split_front_matter(raw)
        name = str(meta.get("name") or sname).strip()
        desc = " ".join(str(meta.get("description") or "").split())
        when = " ".join(str(meta.get("when_to_use") or "").split())
        suffix = f" (when: {when})" if when else ""
        path = f"{root.rstrip('/')}/{rel}" if root else rel
        head, rest = f"- `{name}` ({path}): ", f"{desc}{suffix}"
        if len(head) + len(rest) >= 200:
            rest = rest[:max(0, 196 - len(head))] + "..."
        line = (head + rest).strip()
        entries.append(line)
    if not entries:
        return ""
    header = "\n\n## Available skills (read_file the skill's path when it applies)\n"
    return header + "\n".join(entries)


def harness_contract(cfg: HarnessConfig, configs: ConfigStore | Path | None = None) -> str:
    b = cfg.budget
    lim = []
    if b.tokens:
        lim.append(f"{b.tokens / 1e6:.1f}M tokens")
    if b.cost_usd:
        lim.append(f"cost ${b.cost_usd:.0f}")
    if b.wall_clock_s:
        lim.append(f"wall clock {b.wall_clock_s / 3600:.1f} h")
    if b.max_steps:
        lim.append(f"{b.max_steps} steps")
    if b.on_exhausted == "pause":
        limit_line = (f"- Checkpoints for this activation: {', '.join(lim)}. Reaching one pauses "
                      "the work until the requester extends it; you then continue with the same "
                      "context. Do not shorten or rush the work because of them.")
    else:
        limit_line = (f"- Limits for this activation: {', '.join(lim)}. These are generous safety "
                      "limits, not targets: do the work thoroughly and verify it. You will be "
                      f"notified at {int(b.warn_at * 100)}% and {int(b.decide_at * 100)}% usage.")
    base = f"""

## Harness contract
- You act only through tool calls. Every activation must end with a call to `finish`; a reply without a tool call does not end the work.
{limit_line}
- Older messages may be replaced by a summary when the context grows; use `recall` to recover exact details instead of guessing.
- Long tool outputs are truncated in the middle; use `read_output` to see the omitted part."""  # noqa: E501
    return base + skill_index(configs)


def assemble(system_prompt: str, state: ActivationState) -> list[ChatMessage]:
    msgs = [ChatMessage(role="system", content=system_prompt)]
    msgs += [e.msg for e in state.entries[: state.pinned]]
    if state.summary:
        msgs.append(ChatMessage(role="user", content=SUMMARY_PREFIX + state.summary.summary))
    msgs += [e.msg for e in state.entries[state.first_kept:]]
    return msgs


def fixed_tokens(system_prompt: str, tools: list[ToolSpec]) -> int:
    n = len(system_prompt) + sum(len(t.description) + len(str(t.parameters)) for t in tools)
    return n // 4 + 16
