"""Composition root of layer 3 (docs/40-institution.md §10): loads the rule set, plugs the
InstitutionGate, planners, tools, validation worker and attention into layer 2."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from sciteam.comm.service import CommService
from sciteam.comm.view import CommView
from sciteam.institution.gate import InstitutionGate, act_task
from sciteam.institution.model import Constitution, RuleSet
from sciteam.institution.service import InstitutionService
from sciteam.institution.tools import (
    INSTITUTION_TOOLS,
    awaiting,
    render_record,
    render_waiting_mine,
)
from sciteam.institution.validation import ValidationWorker, digest_tree
from sciteam.institution.view import InstitutionView
from sciteam.kernel.config_store import ConfigStore
from sciteam.kernel.events import Event
from sciteam.providers.sandbox import DockerSandbox, DockerSandboxConfig, LocalSandbox
from sciteam.providers.sandbox.base import Sandbox
from sciteam.runtime.attention import AttentionItem
from sciteam.runtime.config import TeamFile, private_root, validator_private_mounts
from sciteam.runtime.scheduler import Scheduler
from sciteam.runtime.team import RUN_FILE, TeamRun

NOTE_CHARS = 1500


def load_ruleset(configs: ConfigStore, team: TeamFile) -> RuleSet:
    texts = {team.comm_policy: configs.load_text(team.comm_policy)}
    if team.institution:
        texts[team.institution] = configs.load_text(team.institution)
        const = Constitution.model_validate(yaml.safe_load(texts[team.institution]) or {})
        if const.rules:
            texts[const.rules] = configs.load_text(const.rules)
    return RuleSet(team.institution, team.comm_policy, texts)


def git_commit(workspace: Path, ref: str) -> str | None:
    r = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--verify", "--quiet",
                        f"{ref}^{{commit}}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


class InstitutionTeamRun(TeamRun):
    def _load_ruleset(self) -> RuleSet:
        return load_ruleset(self.configs, self.team)

    def _make_comm(self) -> CommService:
        self.ruleset = self._load_ruleset()
        self.inst = InstitutionView(self.team.institution, self.team.comm_policy)
        gate = InstitutionGate(self.inst, lambda ref: git_commit(self.workspace, ref))
        comm = CommService(self.store, self.ruleset.comm, self.clock, extra_gate=gate)
        self.institution = InstitutionService(comm, self.inst)
        return comm

    def on_create(self) -> None:
        self.institution.start(self.ruleset)

    def settle(self) -> None:
        self.institution.settle()

    def extra_tools(self) -> dict[str, Any]:
        return dict(INSTITUTION_TOOLS)

    def tool_extras(self) -> dict[str, Any]:
        return {"institution": self.institution}

    def status_lines(self) -> list[str]:
        svc, inst = self.institution, self.inst
        svc.settle()
        out = [f"institution: rule version {inst.rules.version if inst.rules else '-'}; "
               f"{len(inst.pending)} pending act(s); balances "
               + (", ".join(f"@{m} {v:+g}" for m, v in sorted(inst.totals.items())) or "-")]
        for h in self.comm.view.humans():
            out += [f"  awaiting @{h.id}: {line[2:]}" for line in awaiting(svc, h.id)]
        return out

    def extensions(self, sched: Scheduler) -> list:
        self.institution.version_listeners.append(lambda rs: sched.set_policy(rs.comm))
        root = private_root(self.workspace)
        worker = ValidationWorker(self.institution, workspace=self.workspace, blobs=self.blobs,
                                  sandbox=self._validation_sandbox,
                                  inputs=(lambda: digest_tree(root))
                                  if self.runtime.sandbox.private_mount else None)
        return [InstitutionExtension(self.institution, worker)]

    def _validation_sandbox(self, root: Path) -> Sandbox:
        sc = self.runtime.sandbox
        if sc.kind == "local":
            if sc.private_mount:
                raise ValueError("private_mount needs a docker team sandbox")
            return LocalSandbox(root, init_commands=sc.init_commands)
        return DockerSandbox(DockerSandboxConfig(
            image=sc.image, workdir=sc.workspace_mount, network="none", memory=sc.memory,
            cpus=sc.cpus, init_commands=sc.init_commands,
            mounts=[f"{root.resolve()}:{sc.workspace_mount}",
                    *(_abs_mount(m) for m in sc.validator_mounts),
                    *validator_private_mounts(sc, self.workspace)],
            labels={"sciteam.run": self.run_dir.name, "sciteam.role": "validator"},
            user=f"{os.getuid()}:{os.getgid()}" if sc.user == "auto" else sc.user,
            env={"HOME": "/tmp"}, platform=None))


def _abs_mount(spec: str) -> str:
    host, _, rest = spec.partition(":")
    return f"{Path(host).resolve()}:{rest}"


class InstitutionExtension:
    """Scheduler extension: institutional attention, briefing sections, settle and validation
    on every iteration."""

    def __init__(self, svc: InstitutionService, worker: ValidationWorker):
        self.svc, self.worker = svc, worker

    def step(self) -> None:
        self.svc.settle()
        self.worker.step()

    def busy(self) -> bool:
        return self.worker.busy()

    async def close(self) -> None:
        await self.worker.close()

    def briefing(self, member: str) -> list[tuple[str, str]]:
        rs = self.svc.inst.rules
        if rs is None:
            return []
        principles = rs.constitution.principles.strip() if rs.constitution else ""
        head = (f"{principles}\n\n" if principles else "") + (
            f"Rule version {rs.version}. Your Acts are checked against these rules; `rules` "
            f"shows them in full and how to amend them, `standing` the ledger, `pending` what "
            f"is waiting.")
        out = [("Institution", head), ("Your record", render_record(self.svc, member))]
        todo = awaiting(self.svc, member) + render_waiting_mine(self.svc, member)
        if todo:
            out.append(("Institutional business", "\n".join(todo)))
        return out

    def attention(self, ev: Event, view: CommView) -> list[AttentionItem]:
        p, inst = ev.payload, self.svc.inst
        notes: list[tuple[str, str, str]] = []            # (member, note, ref)
        if ev.type == "act_resolved":
            act = inst.acts.get(p["act_id"])
            what = f"{p['kind']} {act.params.get('transition', '')} " \
                f"{act.params.get('task', '')}".strip() if act else p["kind"]
            reason = f": {p['reason']}" if p.get("reason") else ""
            notes.append((p["actor"], f"Your pending {what} ({p['act_id']}) was "
                          f"{'allowed and took effect' if p['decision'] == 'allow' else 'denied'}"
                          f"{reason}"[:NOTE_CHARS], p["act_id"]))
        elif ev.type == "approval_requested":
            subject = inst.acts.get(p["key"])
            task = act_task(subject, view) if subject else None
            for m in view.members:
                if m != p.get("actor") and self.svc.selector_holds(p["approver"], m, m, task):
                    notes.append((m, f"Awaiting your decision ({p['approval']}): "
                                  f"{p['summary']} — decide_pending(id='{p['key']}', "
                                  f"decision=approve|reject)", p["key"]))
        elif ev.type == "amendment_proposed":
            a = inst.amendments.get(p["amendment_id"])
            for m in (self.svc.voters(a) if a else []):
                if m != p["proposer"]:
                    notes.append((m, f"@{p['proposer']} proposes amendment {a.id} to {a.path}: "
                                  f"{a.rationale[:500]} — see pending, then vote(amendment="
                                  f"'{a.id}', choice=yes|no)", a.id))
        elif ev.type in ("amendment_enacted", "amendment_closed"):
            a = inst.amendments.get(p["amendment_id"])
            if a is not None:
                how = "enacted: the new rules are in force" if ev.type == "amendment_enacted" \
                    else f"closed ({p['reason']}{': ' + p['note'] if p.get('note') else ''})"
                notes.append((a.proposer, f"Your amendment {a.id} to {a.path} was {how}", a.id))
        return [AttentionItem(agent=m, kind="institution", seq=ev.seq, ts=ev.ts, wakes=True,
                              ref=ref, note=note)
                for m, note, ref in notes if m in view.members]


def open_run(run_dir: Path, **kw) -> TeamRun:
    """The run with the layers its team file asks for."""
    meta = json.loads((run_dir / RUN_FILE).read_text())
    team = ConfigStore(meta["configs_dir"], meta.get("overlays", [])).load(meta["team"], TeamFile)
    return InstitutionTeamRun(run_dir, **kw) if team.institution else TeamRun(run_dir, **kw)


def opener_for(configs_dir: Path, team: str, overlays: list[Path] = ()):
    tf = ConfigStore(configs_dir, [str(Path(o).resolve()) for o in overlays]).load(team, TeamFile)
    return InstitutionTeamRun if tf.institution else TeamRun


def decide(run_dir: Path, *, as_: str, id: str, decision: str, note: str = "",
           opener=None) -> str:
    """A human member decides something awaiting approval (40 §6.2); recorded as an Act."""
    from sciteam.comm.model import Act
    from sciteam.kernel.ids import new_id

    tr = (opener or open_run)(run_dir)
    try:
        r = tr.comm.submit(Act(act_id=new_id("act"), actor=as_, kind="decide_pending",
                               params={"id": id, "decision": decision, "note": note}))
        if isinstance(tr, InstitutionTeamRun):
            tr.settle()
        return f"{decision}d {id}" if r.ok else f"denied: {r.decision.reason}"
    finally:
        tr.close()
