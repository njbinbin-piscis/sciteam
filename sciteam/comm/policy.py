"""Communication policy: schema, selector language and PolicyGate (30 §7)."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sciteam.comm.model import HUMAN_ALIAS, Conversation, Decision, Task, deny
from sciteam.comm.view import CommView
from sciteam.kernel.clock import Clock

WAKE_KINDS = ("dm", "mention", "room", "thread_reply", "task_assigned", "task_ready",
              "task_open", "task_review", "task_returned", "task_changed", "timer", "human")


class Rule(BaseModel):
    name: str
    act: str                      # post_message | mention | create_room | add_member | ...
    effect: Literal["allow", "deny"] = "allow"
    via: str = "*"                # post_message only: dm | room | task | "a|b" | *
    transition: str = "*"         # task_transition only
    from_: str = Field("*", alias="from")
    to: str | None = None

    model_config = {"populate_by_name": True}


class RoomSpec(BaseModel):
    name: str
    members: str = "*"            # selector; "{team}" is substituted when for_each: team
    for_each: Literal["team"] | None = None
    topic: str = ""


class Limits(BaseModel):
    message_chars: int
    messages_per_agent_per_min: int
    messages_per_pair_per_10min: int


class CommPolicy(BaseModel):
    name: str
    description: str
    selectors: dict[str, str] = Field(default_factory=dict)
    rules: list[Rule]
    rooms: list[RoomSpec] = Field(default_factory=list)
    wake_on: list[str]
    limits: Limits
    humans_see_all: bool = True

    @model_validator(mode="after")
    def _check(self) -> "CommPolicy":
        bad = [w for w in self.wake_on if w not in WAKE_KINDS]
        if bad:
            raise ValueError(f"unknown wake_on kinds {bad}; allowed {WAKE_KINDS}")
        for r in self.rules:
            compile_selector(r.from_, self.selectors)
            if r.to is not None:
                compile_selector(r.to, self.selectors)
        return self


# ---- selector language --------------------------------------------------------------------

@dataclass
class SelCtx:
    view: CommView
    actor: str
    conv: Conversation | None = None
    task: Task | None = None


Pred = Callable[[str, SelCtx], bool]
_TOKEN = re.compile(r"\s*(\||&|!|\(|\)|[^\s|&!()]+)")
_TASK_FIELDS = {"task.assignee": "assignee", "task.reviewer": "reviewer",
                "task.creator": "created_by"}


class SelectorError(ValueError):
    pass


def _member(ctx: SelCtx, mid: str):
    return ctx.view.members.get(mid)


def member_groups(m) -> set[str]:
    raw = (getattr(m, "attrs", None) or {}).get("groups", "")
    return {g.strip() for g in raw.split(",") if g.strip()}


def _atom(tok: str, named: dict[str, str], depth: int) -> Pred:
    if tok.endswith(":"):
        raise SelectorError(f"selector atom '{tok}' needs a value")
    if tok == "*":
        return lambda x, c: x == HUMAN_ALIAS or x in c.view.members
    if tok in ("human", "kind:human"):
        return lambda x, c: x == HUMAN_ALIAS or getattr(_member(c, x), "kind", "") == "human"
    if tok == "kind:agent":
        return lambda x, c: getattr(_member(c, x), "kind", "") == "agent"
    if tok.startswith("role:"):
        v = tok[5:]
        return lambda x, c: getattr(_member(c, x), "role", None) == v
    if tok.startswith("team:"):
        v = tok[5:]
        return lambda x, c: getattr(_member(c, x), "team", None) == v
    if tok.startswith("group:"):
        v = tok[6:]
        return lambda x, c: v in member_groups(_member(c, x))
    if tok == "self":
        return lambda x, c: x == c.actor
    if tok == "self.manager":
        return lambda x, c: x == getattr(_member(c, c.actor), "manager", None)
    if tok == "self.reports":
        return lambda x, c: getattr(_member(c, x), "manager", None) == c.actor
    if tok == "self.team":
        def team(x: str, c: SelCtx) -> bool:
            me, other = _member(c, c.actor), _member(c, x)
            return bool(me and other and me.team and me.team == other.team)
        return team
    if tok == "self.peers":
        def peers(x: str, c: SelCtx) -> bool:
            me, other = _member(c, c.actor), _member(c, x)
            return bool(me and other and me.manager and x != c.actor
                        and me.manager == other.manager)
        return peers
    if tok == "room.member":
        return lambda x, c: c.conv is not None and x in c.conv.members
    if tok == "task.party":
        return lambda x, c: c.task is not None and x in c.task.parties()
    if tok in _TASK_FIELDS:
        f = _TASK_FIELDS[tok]
        return lambda x, c: c.task is not None and getattr(c.task, f) == x
    if tok == "task.parent_assignee":
        def parent(x: str, c: SelCtx) -> bool:
            p = c.view.tasks.get(c.task.parent) if c.task and c.task.parent else None
            return p is not None and p.assignee == x
        return parent
    if tok.startswith("$"):
        name = tok[1:]
        if name not in named:
            raise SelectorError(f"unknown named selector '{tok}'")
        if depth > 8:
            raise SelectorError("named selectors nest too deeply")
        return compile_selector(named[name], named, depth + 1)
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", tok):
        return lambda x, c: x == tok
    raise SelectorError(f"unknown selector atom '{tok}'")


def compile_selector(src: str, named: dict[str, str] | None = None, depth: int = 0) -> Pred:
    named = named or {}
    toks = _TOKEN.findall(src)
    if not toks:
        raise SelectorError("empty selector")
    pos = 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def expr() -> Pred:
        parts = [term()]
        while peek() == "|":
            take()
            parts.append(term())
        return parts[0] if len(parts) == 1 else (lambda x, c: any(p(x, c) for p in parts))

    def term() -> Pred:
        parts = [factor()]
        while peek() == "&":
            take()
            parts.append(factor())
        return parts[0] if len(parts) == 1 else (lambda x, c: all(p(x, c) for p in parts))

    def factor() -> Pred:
        t = peek()
        if t is None:
            raise SelectorError(f"unexpected end of selector '{src}'")
        if t == "!":
            take()
            inner = factor()
            return lambda x, c: not inner(x, c)
        if t == "(":
            take()
            e = expr()
            if peek() != ")":
                raise SelectorError(f"missing ')' in '{src}'")
            take()
            return e
        if t in ("|", "&", ")"):
            raise SelectorError(f"unexpected '{t}' in '{src}'")
        return _atom(take(), named, depth)

    out = expr()
    if pos != len(toks):
        raise SelectorError(f"trailing tokens in selector '{src}'")
    return out


def _alt(pattern: str, value: str) -> bool:
    return pattern == "*" or value in {p.strip() for p in pattern.split("|")}


# ---- gate ---------------------------------------------------------------------------------

@dataclass
class Check:
    """One policy question: may `actor` do `act` (via/transition) towards `target`?"""
    act: str
    actor: str
    target: str | None = None
    via: str = ""
    transition: str = ""
    conv: Conversation | None = None
    task: Task | None = None


class PolicyGate:
    def __init__(self, policy: CommPolicy, clock: Clock):
        self.policy = policy
        self.clock = clock
        self._rules = [(r, compile_selector(r.from_, policy.selectors),
                        compile_selector(r.to, policy.selectors) if r.to else None)
                       for r in policy.rules]

    def ask(self, chk: Check, view: CommView) -> Decision:
        ctx = SelCtx(view=view, actor=chk.actor, conv=chk.conv, task=chk.task)
        for rule, frm, to in self._rules:
            if rule.act != chk.act:
                continue
            if chk.act == "post_message" and not _alt(rule.via, chk.via):
                continue
            if chk.act == "task_transition" and not _alt(rule.transition, chk.transition):
                continue
            if not frm(chk.actor, ctx):
                continue
            if to is not None and (chk.target is None or not to(chk.target, ctx)):
                continue
            if rule.effect == "deny":
                return deny(self._describe(chk), rule.name)
            return Decision(decision="allow", rule=rule.name)
        return deny(self._describe(chk) + " (no rule allows it)")

    @staticmethod
    def _describe(chk: Check) -> str:
        what = chk.act
        if chk.transition:
            what += f" {chk.transition}"
        if chk.via:
            what += f" via {chk.via}"
        if chk.target:
            what += f" -> {chk.target}"
        return f"{chk.actor} may not {what}"

    def can(self, chk: Check, view: CommView) -> bool:
        return self.ask(chk, view).decision == "allow"

    # ---- rate limits (humans exempt) ---------------------------------------------------
    def rate(self, actor: str, conv: Conversation, view: CommView) -> Decision | None:
        m = view.members.get(actor)
        if m is None or m.kind == "human":
            return None
        lim = self.policy.limits
        now = self.clock.now()
        minute = now - timedelta(minutes=1)
        mine = [t for t, a, _ in view.post_times if a == actor and t >= minute]
        if len(mine) >= lim.messages_per_agent_per_min:
            wait = int((mine[0] - minute).total_seconds()) + 1
            return deny(f"rate limit: at most {lim.messages_per_agent_per_min} messages per "
                        f"minute; retry in {wait}s", "limits.messages_per_agent_per_min")
        if conv.kind == "dm":
            ten = now - timedelta(minutes=10)
            pair = [t for t, _, c in view.post_times if c == conv.id and t >= ten]
            if len(pair) >= lim.messages_per_pair_per_10min:
                return deny(f"rate limit: at most {lim.messages_per_pair_per_10min} messages "
                            f"per 10 minutes in one private conversation; wrap up the exchange "
                            f"or move on", "limits.messages_per_pair_per_10min")
        return None
