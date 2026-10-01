"""Communication data model (docs/30-communication.md §2–§6)."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MemberKind = Literal["agent", "human"]
ConvKind = Literal["dm", "room", "task"]
DepType = Literal["FS", "SS", "FF", "SF"]
TaskStatus = Literal["open", "assigned", "in_progress", "blocked", "in_review", "done",
                     "cancelled"]
TERMINAL = ("done", "cancelled")
STARTED = ("in_progress", "blocked", "in_review", "done")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MENTION_RE = re.compile(r"(?<![\w@./])@([a-z0-9][a-z0-9_-]*)")
HUMAN_ALIAS = "human"


class Member(BaseModel):
    id: str
    kind: MemberKind = "agent"
    role: str = ""
    team: str = ""
    manager: str | None = None
    harness: str | None = None
    profile: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)


class Attachment(BaseModel):
    path: str | None = None       # workspace-relative path
    blob: str | None = None       # BlobStore sha256
    note: str = ""


class Conversation(BaseModel):
    id: str
    kind: ConvKind
    members: set[str] = Field(default_factory=set)
    topic: str = ""
    created_by: str
    n_messages: int = 0
    last_seq: int = 0


class Message(BaseModel):
    msg_id: str
    conv: str
    author: str
    text: str
    mentions: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    thread: str
    attachments: list[Attachment] = Field(default_factory=list)
    seq: int = 0
    ts: datetime | None = None


class Dependency(BaseModel):
    task: str
    type: DepType = "FS"


class TaskEvent(BaseModel):
    seq: int
    ts: datetime
    actor: str
    transition: str
    frm: str
    to: str
    note: str = ""


class Task(BaseModel):
    id: str
    short: str                    # T<n>, for display and as an alias in tool arguments
    title: str
    description: str = ""
    parent: str | None = None
    deps: list[Dependency] = Field(default_factory=list)
    status: TaskStatus = "open"
    created_by: str
    assignee: str | None = None
    reviewer: str | None = None
    watchers: set[str] = Field(default_factory=set)
    estimate_h: float | None = None
    labels: list[str] = Field(default_factory=list)
    acceptance: str | None = None  # acceptance command, written by the task's creator
    artifacts: list[Attachment] = Field(default_factory=list)
    block_reason: str = ""
    history: list[TaskEvent] = Field(default_factory=list)

    @property
    def conv(self) -> str:
        return f"task:{self.id}"

    def parties(self) -> set[str]:
        p = {self.created_by} | self.watchers
        if self.assignee:
            p.add(self.assignee)
        if self.reviewer:
            p.add(self.reviewer)
        return p


class Act(BaseModel):
    act_id: str
    actor: str
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    activation_id: str | None = None
    rule_version: str | None = None


class Decision(BaseModel):
    decision: Literal["allow", "deny", "pending"]
    reason: str = ""
    rule: str | None = None
    waiting: list[str] = Field(default_factory=list)   # what a pending decision waits for


ALLOW = Decision(decision="allow")


def deny(reason: str, rule: str | None = None) -> Decision:
    return Decision(decision="deny", reason=reason, rule=rule)


def dm_id(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return f"dm:{x}+{y}"


def extract_mentions(text: str, known: set[str]) -> list[str]:
    """@id tokens that name a known member (or the human alias); order-preserving, unique."""
    out: list[str] = []
    for m in MENTION_RE.finditer(text):
        name = m.group(1).rstrip("-_")
        if (name in known or name == HUMAN_ALIAS) and name not in out:
            out.append(name)
    return out
