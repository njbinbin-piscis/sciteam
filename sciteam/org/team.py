"""Composition root of layer 4 (docs/50-organization.md §7): the manifest joins the rule set;
OrgService adds hire/dismiss and the consequences of the organization in force."""

import json
from pathlib import Path
from typing import Any

from sciteam.comm.service import CommService
from sciteam.comm.view import CommView
from sciteam.institution.model import RuleSet
from sciteam.institution.team import InstitutionTeamRun, load_ruleset
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import Event
from sciteam.org.service import OrgService
from sciteam.org.tools import ORG_TOOLS, render_chart, render_goals, render_org
from sciteam.runtime.attention import AttentionItem
from sciteam.runtime.config import TeamFile
from sciteam.runtime.scheduler import Scheduler
from sciteam.runtime.team import RUN_FILE, TeamRun


class OrgTeamRun(InstitutionTeamRun):
    def _load_ruleset(self) -> RuleSet:
        rs = load_ruleset(self.configs, self.team)
        return rs.with_file(self.team.org, self.configs.load_text(self.team.org))

    def _make_comm(self) -> CommService:
        comm = super()._make_comm()
        self.org = OrgService(comm, self.institution, self.configs, self.team.org)
        return comm

    def on_create(self) -> None:
        super().on_create()
        self.org.settle()

    def settle(self) -> None:
        super().settle()
        self.org.settle()

    def extra_tools(self) -> dict[str, Any]:
        return {**super().extra_tools(), **ORG_TOOLS}

    def tool_extras(self) -> dict[str, Any]:
        return {**super().tool_extras(), "org": self.org}

    def status_lines(self) -> list[str]:
        out = super().status_lines()
        self.org.settle()
        return out + ["organization:", render_chart(self.org), "goals:", render_goals(self.org)]

    def extensions(self, sched: Scheduler) -> list:
        return [*super().extensions(sched), OrgExtension(self.org)]


class OrgExtension:
    def __init__(self, svc: OrgService):
        self.svc = svc

    def step(self) -> None:
        self.svc.settle()

    def busy(self) -> bool:
        return False

    def stop_reason(self) -> str | None:
        return self.svc.stop_reason()

    async def close(self) -> None:
        return None

    def briefing(self, member: str) -> list[tuple[str, str]]:
        if self.svc.manifest is None:
            return []
        return [("Organization", render_org(self.svc, member)
                 + "\n\n`org` shows this again; `profiles` lists whom one can hire.")]

    def attention(self, ev: Event, view: CommView) -> list[AttentionItem]:
        p, svc = ev.payload, self.svc
        notes: list[tuple[str, str]] = []
        man = svc.manifest
        if ev.type == "org_enacted" and p.get("vacancies") and man and man.staffing:
            vac = ", ".join(f"{s} ×{n}" for s, n in p["vacancies"].items())
            for m in view.members:
                if svc.isvc.selector_holds(man.staffing, m, m):
                    notes.append((m, f"The organization in force has vacant seats: {vac}. "
                                  f"See `org` and `profiles`; fill them with `hire`."))
        elif ev.type == "member_registered" and p.get("hired_by"):
            m = p["member"]
            notes.append((m["id"], f"You were hired by @{p['hired_by']} as {p['seat']}. Your "
                          f"briefing has the mission and your seat's charter; see `org` for "
                          f"the organization and start on your responsibilities."))
        elif ev.type in ("goal_met", "goal_reopened"):
            what = "met: the run stops" if ev.type == "goal_met" else "no longer met"
            for h in view.humans():
                notes.append((h.id, f"Goal(s) {', '.join(p['goals'])} {what}."))
        return [AttentionItem(agent=m, kind="institution", seq=ev.seq, ts=ev.ts, wakes=True,
                              ref=f"org:{ev.seq}", note=note)
                for m, note in notes if m in view.members]


def _team(run_dir: Path) -> TeamFile:
    meta = json.loads((run_dir / RUN_FILE).read_text())
    return ConfigStore(meta["configs_dir"], meta.get("overlays", [])).load(meta["team"], TeamFile)


def _cls(team: TeamFile) -> type[TeamRun]:
    if team.org:
        return OrgTeamRun
    return InstitutionTeamRun if team.institution else TeamRun


def open_run(run_dir: Path, **kw) -> TeamRun:
    """The run with the layers its team file asks for."""
    return _cls(_team(run_dir))(run_dir, **kw)


def opener_for(configs_dir: Path, team: str, overlays: list[Path] = ()):
    cs = ConfigStore(configs_dir, [str(Path(o).resolve()) for o in overlays])
    return _cls(cs.load(team, TeamFile))

