"""Notes that make later work smarter: the project's own (written by its members and owner,
in force at once) and the platform's (library memory entries, frozen into the run's configs).

A note is a markdown file with a YAML header: id, title, scope, optional `when` (a regex the
failing tool's output must match), author, created, from_user, share, sources."""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from sciteam.kernel.config_store import split_front_matter
from sciteam.kernel.ids import new_id

FORGOTTEN = ".forgotten"
_SCOPE = re.compile(r"^(all|seat:[\w.-]+|tool:[\w.-]+)$")


class MemoryConfig(BaseModel):
    briefing_max_notes: int = 30
    note_max_chars: int = 800
    notes_per_tool_error: int = 3


class Note(BaseModel):
    id: str
    title: str = ""
    body: str
    scope: str = "all"
    when: str | None = None
    author: str = ""
    created: str = ""
    origin: str = "project"          # project | platform
    from_user: bool = False
    share: bool = False              # proposed for the platform library at acceptance
    sources: list[dict[str, Any]] = Field(default_factory=list)

    def applies_to(self, member: str, role: str) -> bool:
        return self.scope == "all" or self.scope in (f"seat:{member}", f"seat:{role}")

    def line(self, max_chars: int) -> str:
        text = (f"{self.title}: " if self.title else "") + " ".join(self.body.split())
        if len(text) > max_chars:
            text = text[:max_chars] + " …"
        tag = ("platform · owner" if self.from_user else "platform") if self.origin == "platform" \
            else ("owner" if self.from_user else f"@{self.author}" if self.author else "project")
        return f"- [{self.id} · {tag}{'' if self.scope == 'all' else ' · ' + self.scope}] {text}"


def valid_scope(scope: str) -> bool:
    return bool(_SCOPE.match(scope))


def parse_note(text: str, stem: str, origin: str) -> Note | None:
    try:
        head, body = split_front_matter(text)
    except yaml.YAMLError:
        return None
    if not body.strip():
        return None
    head = {k: v for k, v in head.items() if k in Note.model_fields and k not in ("body", "origin")}
    head.setdefault("id", stem)
    try:
        return Note(**head, body=body.strip(), origin=origin)
    except ValueError:
        return None


def read_note(path: Path, origin: str) -> Note | None:
    try:
        return parse_note(path.read_text(encoding="utf-8"), path.stem, origin)
    except OSError:
        return None


def note_text(note: Note) -> str:
    head = note.model_dump(exclude={"body", "origin"}, exclude_none=True)
    return "---\n" + yaml.safe_dump(head, allow_unicode=True, sort_keys=False) + "---\n" \
        + note.body.strip() + "\n"


class MemoryBook:
    """The project's notes (writable) over the platform's (read-only here); a forgotten note,
    of either origin, stops loading in this project."""

    def __init__(self, project_dir: Path | None, platform_dir: Path | None = None,
                 cfg: MemoryConfig | None = None):
        self.project_dir = Path(project_dir) if project_dir else None
        self.platform_dir = Path(platform_dir) if platform_dir else None
        self.cfg = cfg or MemoryConfig()

    def _forgotten(self) -> set[str]:
        d = self.project_dir / FORGOTTEN if self.project_dir else None
        return {p.stem for p in d.glob("*.md")} if d and d.is_dir() else set()

    def notes(self) -> list[Note]:
        gone = self._forgotten()
        out: list[Note] = []
        for d, origin in ((self.platform_dir, "platform"), (self.project_dir, "project")):
            if d is None or not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                n = read_note(p, origin)
                if n is not None and n.id not in gone:
                    out.append(n)
        return out

    def get(self, nid: str) -> Note | None:
        return next((n for n in self.notes() if n.id == nid), None)

    def for_member(self, member: str, role: str) -> list[Note]:
        return [n for n in self.notes() if n.applies_to(member, role)]

    def for_tool(self, tool: str, output: str) -> list[Note]:
        out = []
        for n in self.notes():
            if n.scope != f"tool:{tool}":
                continue
            if n.when:
                try:
                    if not re.search(n.when, output or ""):
                        continue
                except re.error:
                    continue
            out.append(n)
        return out

    def remember(self, body: str, *, scope: str = "all", title: str = "", when: str | None = None,
                 author: str = "", from_user: bool = False, share: bool = False,
                 sources: list[dict[str, Any]] | None = None, nid: str | None = None) -> Note:
        if self.project_dir is None:
            raise ValueError("this run keeps no project memory")
        if not body.strip():
            raise ValueError("a note needs text")
        if not valid_scope(scope):
            raise ValueError("scope is all, seat:<member or role>, or tool:<tool name>")
        if when:
            if not scope.startswith("tool:"):
                raise ValueError("when applies only to a tool: scope")
            re.compile(when)
        note = Note(id=nid or new_id("mem"), title=title.strip(), body=body.strip(), scope=scope,
                    when=when or None, author=author, from_user=from_user, share=share,
                    created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    sources=list(sources or []))
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / f"{note.id}.md").write_text(note_text(note), encoding="utf-8")
        return note

    def forget(self, nid: str, *, reason: str, by: str) -> Note:
        if self.project_dir is None:
            raise ValueError("this run keeps no project memory")
        note = self.get(nid)
        if note is None:
            raise ValueError(f"no note {nid} in force")
        if not reason.strip():
            raise ValueError("say why the note no longer holds")
        gone = self.project_dir / FORGOTTEN
        gone.mkdir(parents=True, exist_ok=True)
        kept = note.model_copy(update={"sources": [*note.sources, {
            "forgotten_by": by, "reason": reason.strip(),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}]})
        (gone / f"{nid}.md").write_text(note_text(kept), encoding="utf-8")
        own = self.project_dir / f"{nid}.md"
        if note.origin == "project" and own.exists():
            own.unlink()
        return note

    def forgotten(self) -> list[Note]:
        d = self.project_dir / FORGOTTEN if self.project_dir else None
        if not d or not d.is_dir():
            return []
        return [n for p in sorted(d.glob("*.md")) if (n := read_note(p, "forgotten"))]


def render_memory(notes: list[Note], cfg: MemoryConfig) -> str:
    """The briefing section: notes for everyone and for this seat; tool notes appear when that
    tool fails."""
    general = [n for n in notes if not n.scope.startswith("tool:")]
    tools = sorted({n.scope[5:] for n in notes if n.scope.startswith("tool:")})
    shown = general[-cfg.briefing_max_notes:]
    lines = [n.line(cfg.note_max_chars) for n in shown]
    if len(general) > len(shown):
        lines.insert(0, f"({len(general) - len(shown)} older notes not shown)")
    if tools:
        lines.append(f"(notes about {', '.join(tools)} appear when that tool fails)")
    lines.append("remember(text, scope) records a lesson for the rest of this project at once "
                 "(a pitfall, a parameter that works, a convention the owner asked for); "
                 "forget(id, reason) drops a note that turned out wrong or outdated.")
    return "\n".join(lines)


class MemoryView:
    """One activation's handle on the book: who writes, and which tool notes it has seen."""

    def __init__(self, book: MemoryBook, member: str, role: str, project: str = "",
                 activation: str = ""):
        self.book, self.member, self.role = book, member, role
        self.project, self.activation = project, activation
        self.shown: set[str] = set()

    def on_tool_error(self, tool: str, output: str) -> str:
        notes = [n for n in self.book.for_tool(tool, output) if n.id not in self.shown]
        notes = notes[: self.book.cfg.notes_per_tool_error]
        if not notes:
            return ""
        self.shown.update(n.id for n in notes)
        return (f"\n[memory] Notes about {tool} from earlier work:\n"
                + "\n".join(n.line(self.book.cfg.note_max_chars) for n in notes))
