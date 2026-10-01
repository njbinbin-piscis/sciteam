"""OrgService: planners of hire and dismiss, and the consequences of the organization in force —
member groups and reporting lines, policy rooms, `org_enacted`, goals and the stop condition
(docs/50-organization.md §3–§7). Every event goes through CommService (`submit`, `record`)."""

import difflib
import re
from typing import Any

from sciteam.comm.model import ID_RE, Act, Member
from sciteam.comm.policy import Check, SelCtx, compile_selector
from sciteam.comm.service import CommService, Plan
from sciteam.harness import HarnessConfig
from sciteam.institution.service import InstitutionService, must
from sciteam.kernel.config_store import ConfigStore, split_front_matter
from sciteam.org.model import Manifest, Seat, sha

ORG_STREAM = "org"
ORG_EVENT_TYPES = {"org_enacted", "goal_met", "goal_reopened", "member_registered"}
TEMPLATE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
UNASSIGN_FROM = ("assigned", "in_progress", "blocked")


class OrgService:
    def __init__(self, comm: CommService, isvc: InstitutionService, configs: ConfigStore,
                 manifest_path: str, templates_dir: str = "agents"):
        self.comm, self.isvc, self.inst = comm, isvc, isvc.inst
        self.configs, self.path, self.templates_dir = configs, manifest_path, templates_dir
        self.enacted_sha: str | None = None
        self.goal_met = False
        self.ever: set[str] = set()                 # every member id ever registered
        self.hires: dict[str, dict[str, Any]] = {}  # member -> hiring record
        self._cursor = 0
        comm.extra_kinds.update({"hire": self._plan_hire, "dismiss": self._plan_dismiss})
        comm.gated_kinds |= {"hire", "dismiss"}
        isvc.amendment_checks.append(self._check_change)
        comm.followers.append(self._follow)
        comm.catch_up()

    # ---- fold ---------------------------------------------------------------------------
    def _follow(self) -> None:
        for ev in self.comm.store.read_global(after_seq=self._cursor, types=ORG_EVENT_TYPES):
            self._cursor = ev.seq
            p = ev.payload
            if ev.type == "org_enacted":
                self.enacted_sha = p["manifest_sha"]
            elif ev.type in ("goal_met", "goal_reopened"):
                self.goal_met = ev.type == "goal_met"
            elif ev.type == "member_registered":
                self.ever.add(p["member"]["id"])
                if p.get("seat"):
                    self.hires[p["member"]["id"]] = {**p, "seq": ev.seq}

    @property
    def manifest(self) -> Manifest | None:
        rs = self.inst.rules
        return rs.extra.get(self.path) if rs is not None else None

    def seated(self, seat: str) -> list[str]:
        return [m.id for m in self.comm.view.members.values()
                if m.kind == "agent" and m.role == seat]

    def goals(self, include_cancelled: bool = False):
        spec = self.manifest.goals if self.manifest else None
        if spec is None:
            return []
        return [t for t in self.comm.view.tasks.values()
                if t.parent is None and spec.label in t.labels
                and (include_cancelled or t.status != "cancelled")]

    def stop_reason(self) -> str | None:
        if self.goal_met:
            return "goal_met"
        if self.goals(include_cancelled=True) and not self.goals():
            return "goals_cancelled"
        return None

    # ---- settle -------------------------------------------------------------------------
    def settle(self) -> int:
        n = 0
        with self.comm.lock:
            self.comm.catch_up()
            if self.manifest is None:
                return 0
            n += self._sync_members() + self._enacted() + self._rooms() + self._goals()
        return n

    def _cause(self) -> int | None:
        return self.inst.versions[-1][0] if self.inst.versions else None

    def _desired(self, m: Member, man: Manifest) -> dict[str, Any]:
        seat = man.seat(m.role)
        if seat is None:
            return {"attrs": {k: v for k, v in m.attrs.items() if k != "groups"}}
        boss = next((b for b in self.seated(seat.reports_to) if b != m.id), None) \
            if seat.reports_to else None
        attrs = {k: v for k, v in m.attrs.items() if k != "groups"}
        if seat.groups:
            attrs["groups"] = ",".join(seat.groups)
        return {"attrs": attrs, "team": seat.team, "manager": boss}

    def _sync_members(self) -> int:
        man, n = self.manifest, 0
        for m in list(self.comm.view.members.values()):
            if m.kind != "agent":
                continue
            want = self._desired(m, man)
            fields = {k: v for k, v in want.items() if getattr(m, k) != v}
            if fields:
                self.comm.record("roster", "member_updated", {"id": m.id, "fields": fields},
                                 causation=self._cause())
                n += 1
        return n

    def _enacted(self) -> int:
        rs = self.inst.rules
        msha = rs.files.get(self.path)
        if msha is None or msha == self.enacted_sha:
            return 0
        seats = {s.name: {"count": s.count, "filled": self.seated(s.name)}
                 for s in self.manifest.seats}
        vacancies = {s: v["count"] - len(v["filled"]) for s, v in seats.items()
                     if v["count"] > len(v["filled"])}
        self.comm.record(ORG_STREAM, "org_enacted", {
            "rule_version": rs.version, "manifest_sha": msha, "seats": seats,
            "vacancies": vacancies}, causation=self._cause())
        return 1

    def _rooms(self) -> int:
        """Rooms the policy in force declares exist and hold everyone its selector names."""
        view, n = self.comm.view, 0
        for spec in self.comm.policy.rooms:
            teams = sorted({m.team for m in view.members.values() if m.team}) \
                if spec.for_each == "team" else [None]
            for team in teams:
                name = spec.name.replace("{team}", team or "")
                sel = compile_selector(spec.members.replace("{team}", team or ""),
                                       self.comm.policy.selectors)
                ctx = SelCtx(view=view, actor="system")
                want = {m for m in view.members if sel(m, ctx)}
                cid = f"room:{name}"
                conv = view.convs.get(cid)
                if conv is None:
                    if want:
                        self.comm.system_room(name, want, spec.topic)
                        n += 1
                    continue
                for m in sorted(want - conv.members):
                    self.comm.record(f"conv/{cid}", "conv_member_added",
                                     {"conv": cid, "member": m}, causation=self._cause())
                    n += 1
        return n

    def _goals(self) -> int:
        spec = self.manifest.goals
        if spec is None:
            return 0
        goals = self.goals()
        met = bool(goals) and all(self.inst.has_fact(spec.fact, t.id) for t in goals)
        if met == self.goal_met:
            return 0
        self.comm.record(ORG_STREAM, "goal_met" if met else "goal_reopened",
                         {"goals": sorted(t.id for t in goals), "fact": spec.fact},
                         causation=self.inst.cursor)
        return 1

    # ---- amendments must fit the people in post -----------------------------------------
    def _check_change(self, path: str, new) -> str | None:
        if path != self.path:
            return None
        man: Manifest = new.extra[path]
        for m in self.comm.view.members.values():
            if m.kind != "agent" or self.manifest is None or self.manifest.seat(m.role) is None:
                continue
            seat, filled = man.seat(m.role), len(self.seated(m.role))
            if seat is None:
                return f"seat '{m.role}' is held by @{m.id}; dismiss before removing the seat"
            if seat.count < filled:
                return f"seat '{m.role}' has {filled} members in post; count {seat.count} " \
                       f"is too small"
        return None

    # ---- planners -----------------------------------------------------------------------
    def template(self, name: str) -> tuple[str, dict, str]:
        """(full text, header, body) of a profile template."""
        must(bool(TEMPLATE_RE.match(name)), f"invalid template name '{name}'", "structure")
        try:
            text = self.configs.load_text(f"{self.templates_dir}/{name}.md")
        except FileNotFoundError:
            text = None
        must(text is not None, f"unknown template '{name}'; see `profiles`", "structure")
        head, body = split_front_matter(text)
        return text, head, body

    def _seat_of(self, name: str) -> Seat:
        man = self.manifest
        must(man is not None, "there is no organization manifest in force", "structure")
        seat = man.seat(name)
        must(seat is not None, f"no seat '{name}' in the organization in force; seats: "
             f"{[s.name for s in man.seats]}", "org")
        return seat

    def _plan_hire(self, act: Act) -> Plan:
        p = act.params
        seat = self._seat_of(str(p.get("seat", "")).strip())
        filled = self.seated(seat.name)
        must(len(filled) < seat.count,
             f"seat '{seat.name}' is full ({seat.count}): {filled}", "org")
        mid = str(p.get("member", "")).strip()
        must(bool(ID_RE.match(mid)) and mid != "human",
             f"invalid member id '{mid}' (lowercase letters, digits, - and _)", "structure")
        must(mid not in self.ever, f"the id '{mid}' is already taken", "structure")
        tname = str(p.get("template", "")).strip()
        text, _, body = self.template(tname)
        profile = str(p.get("profile") or "").strip() or body.strip()
        allowed = self.manifest.allowed_harness(seat)
        harness = str(p.get("harness") or allowed[0]).strip()
        must(harness in allowed, f"seat '{seat.name}' allows harness {allowed}, not "
             f"'{harness}'", "org")
        try:
            self.configs.load(harness, HarnessConfig)
        except Exception as e:  # noqa: BLE001 — any load failure makes the hire invalid
            must(False, f"harness '{harness}' cannot be loaded: {e}", "structure")
        self.comm.policy_check(Check(act="hire", actor=act.actor))
        boss = next(iter(self.seated(seat.reports_to)), None) if seat.reports_to else None
        member = Member(id=mid, kind="agent", role=seat.name, team=seat.team, manager=boss,
                        harness=harness, profile=profile,
                        attrs={"groups": ",".join(seat.groups)} if seat.groups else {})
        diff = "".join(difflib.unified_diff(body.strip().splitlines(True),
                                            profile.splitlines(True),
                                            f"template {tname}", f"profile of {mid}"))
        plan = Plan()
        plan.add("roster", "member_registered", {
            "member": member.model_dump(mode="json"), "hired_by": act.actor,
            "seat": seat.name, "template": tname, "template_sha": sha(text),
            "profile_sha": sha(profile), "adapted": bool(diff),
            "note": str(p.get("note", ""))})
        plan.data = {"member": mid, "seat": seat.name, "harness": harness,
                     "diff": diff[:4000]}
        return plan

    def _plan_dismiss(self, act: Act) -> Plan:
        p, view = act.params, self.comm.view
        mid = str(p.get("member", "")).strip().lstrip("@")
        m = view.members.get(mid)
        must(m is not None and m.kind == "agent", f"no agent member '{mid}'", "structure")
        must(mid != act.actor, "you cannot dismiss yourself", "structure")
        reason = str(p.get("reason", "")).strip()
        must(bool(reason), "dismissal needs a reason", "structure")
        self.comm.policy_check(Check(act="dismiss", actor=act.actor, target=mid))
        plan = Plan()
        plan.add("roster", "member_removed", {"id": mid, "by": act.actor, "reason": reason})
        for t in view.tasks.values():
            if t.assignee == mid and t.status in UNASSIGN_FROM:
                plan.add(f"task/{t.id}", "task_status_changed", {
                    "task_id": t.id, "transition": "unassign", "frm": t.status, "to": "open",
                    "note": f"@{mid} was dismissed", "assignee": None})
        plan.data = {"member": mid}
        return plan

