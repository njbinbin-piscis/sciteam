"""Composition root of layer 3 (docs/40-institution.md §10): loads the rule set, plugs the
InstitutionGate, planners, tools, validation worker and attention into layer 2."""

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml

from sciteam.comm.policy import SelCtx, SelectorError, compile_selector
from sciteam.comm.service import CommService
from sciteam.comm.view import CommView
from sciteam.institution.amend import render_review
from sciteam.institution.claims import ClaimWorker, ReplicationWorker, replicate
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
from sciteam.runtime.realworld import PIP_LOCK, restore_commands
from sciteam.runtime.scheduler import Scheduler
from sciteam.runtime.team import RUN_FILE, TeamRun

NOTE_CHARS = 1500
REPLICATE_ENV = "SCITEAM_REPLICATE_EVERY_S"   # 0 turns the periodic replication off
DAY_S = 86400.0


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


def _named(command: str) -> set[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return {t.removeprefix("/workspace/").removeprefix("./") for t in tokens}


def _git_files(workspace: Path, *args: str) -> set[str]:
    r = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True)
    return set(r.stdout.splitlines()) if r.returncode == 0 else set()


def uncommitted_named(workspace: Path, sha: str, command: str) -> list[str]:
    """Workspace files that `command` names as an argument and commit `sha` does not hold."""
    named = _named(command)
    now = _git_files(workspace, "ls-files", "--cached", "--others", "--exclude-standard")
    if not named & now:
        return []
    return sorted((named & now) - _git_files(workspace, "ls-tree", "-r", "--name-only", sha))


def committed_named(workspace: Path, sha: str, command: str) -> list[str]:
    """Files of commit `sha` that `command` names as an argument."""
    return sorted(_named(command) & _git_files(workspace, "ls-tree", "-r", "--name-only", sha))


class InstitutionTeamRun(TeamRun):
    def _load_ruleset(self) -> RuleSet:
        return load_ruleset(self.configs, self.team)

    def _make_comm(self) -> CommService:
        self.ruleset = self._load_ruleset()
        self.inst = InstitutionView(self.team.institution, self.team.comm_policy)
        gate = InstitutionGate(self.inst, lambda ref: git_commit(self.workspace, ref),
                               lambda sha, cmd: uncommitted_named(self.workspace, sha, cmd),
                               lambda sha, cmd: committed_named(self.workspace, sha, cmd),
                               self.runtime.sandbox.private_mount or None)
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
        sc = self.runtime.sandbox
        claims = ClaimWorker(self.institution, workspace=self.workspace,
                             sandbox=self._validation_sandbox,
                             private=(root, sc.private_mount) if sc.private_mount else None)
        replication = ReplicationWorker(self.institution, workspace=self.workspace,
                                        sandbox=self._validation_sandbox,
                                        every_s=float(os.environ.get(REPLICATE_ENV, DAY_S)))
        return [InstitutionExtension(self.institution, worker, claims, replication)]

    async def replicate_now(self) -> list[str]:
        """Replay every accepted claim once; returns the defect records written."""
        return await replicate(self.institution, self.institution.accepted_claims(),
                               workspace=self.workspace, sandbox=self._validation_sandbox)

    def _rebuild_commands(self, root: Path) -> list[str]:
        return _rebuild_commands_for(self.runtime.mode, root)

    def _validation_sandbox(self, root: Path, member: str | None = None) -> Sandbox:
        sc = self.runtime.sandbox
        if sc.kind == "local":
            if sc.private_mount:
                raise ValueError("private_mount needs a docker team sandbox")
            return LocalSandbox(root, init_commands=sc.init_commands)
        return DockerSandbox(DockerSandboxConfig(
            image=sc.image, workdir=sc.workspace_mount, network="none", memory=sc.memory,
            cpus=sc.cpus, init_commands=[*sc.init_commands, *self._rebuild_commands(root)],
            mounts=[f"{root.resolve()}:{sc.workspace_mount}",
                    *([f"{(root / 'data' / 'incoming').resolve()}:{sc.workspace_mount.rstrip('/')}/data/incoming:ro"]
                      if (root / "data" / "incoming").is_dir() else []),
                    *(_abs_mount(m) for m in sc.validator_mounts),
                    *validator_private_mounts(sc, self.workspace, member)],
            labels={"sciteam.run": self.run_dir.name, "sciteam.role": "validator"},
            user=f"{os.getuid()}:{os.getgid()}" if sc.user == "auto" else sc.user,
            env={"HOME": "/tmp"}, platform=None))


def _rebuild_commands_for(mode: str, root: Path) -> list[str]:
    """Realworld: the offline validator rebuilds what the members installed from the lockfile."""
    lock = root / PIP_LOCK
    if mode != "realworld" or not lock.is_file():
        return []
    return restore_commands(lock.read_text())


def _abs_mount(spec: str) -> str:
    host, _, rest = spec.partition(":")
    return f"{Path(host).resolve()}:{rest}"


class InstitutionExtension:
    """Scheduler extension: institutional attention, briefing sections, settle and validation
    on every iteration."""

    def __init__(self, svc: InstitutionService, worker: ValidationWorker,
                 claims: ClaimWorker | None = None,
                 replication: ReplicationWorker | None = None):
        self.svc, self.worker, self.claims = svc, worker, claims
        self.workers = [w for w in (worker, claims, replication) if w is not None]

    def step(self) -> None:
        self.svc.settle()
        for w in self.workers:
            w.step()

    def busy(self) -> bool:
        return any(w.busy() for w in self.workers)

    def owner_waits(self) -> list[tuple[str, float]]:
        """Approvals only a human can give that are still open, with when each was asked."""
        view = self.svc.comm.view
        humans = [m.id for m in view.members.values() if m.kind == "human"]
        ctx = SelCtx(view=view, actor="system")
        out = []
        for a in self.svc.inst.approvals.values():
            if a.status != "requested":
                continue
            try:
                sel = compile_selector(a.approver)
                if any(sel(m, ctx) for m in humans):
                    out.append((f"approval:{a.key}", a.requested_at))
            except (SelectorError, KeyError, AttributeError):
                continue
        return out

    async def close(self) -> None:
        for w in self.workers:
            await w.close()

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
        elif ev.type in ("amendment_proposed", "amendment_reviewed"):
            a = inst.amendments.get(p["amendment_id"])
            if ev.type == "amendment_proposed" and p.get("trial"):
                a = None
            for m in (self.svc.voters(a) if a else []):
                if m != a.proposer:
                    notes.append((m, f"@{a.proposer} proposes amendment {a.id} to {a.path}: "
                                  f"{a.rationale[:500]} — see pending, then vote(amendment="
                                  f"'{a.id}', choice=yes|no)", a.id))
            if a is not None and ev.type == "amendment_reviewed":
                notes.append((a.proposer, f"The trial validations of your amendment {a.id} "
                              f"finished and it meets admission; voting is open.\n"
                              f"{render_review(p['review'])}"[:NOTE_CHARS], a.id))
        elif ev.type == "amendment_voted":
            a = inst.amendments.get(p["amendment_id"])
            if a is not None and p["voter"] != a.proposer:
                why = f": {p['note']}" if p.get("note") else ""
                notes.append((a.proposer, f"@{p['voter']} voted {p['choice']} on your amendment "
                              f"{a.id}{why}"[:NOTE_CHARS], a.id))
        elif ev.type == "claim_replayed":
            c = inst.claims.get(p["claim_id"])
            if c is not None:
                how = "replayed: the reproduction gives the recorded value" if p["code"] == "ok" \
                    else (f"did not replay ({p['code']}"
                          + (f", reproduction gave {p['observed']!r}"
                             if p.get("observed") is not None else "")
                          + f"); output ends with:\n{(p.get('tail') or '')[-800:]}")
                if p["code"] == "mismatch":
                    how += (f"\nThe recorded value differs from what this commit's reproduction "
                            f"writes. When the reproduction is right, record_claim "
                            f"{c.name} again with value={p['observed']!r} and commit="
                            f"{c.commit[:10]}; the files need no change")
                notes.append((c.actor, f"Your claim {c.id} {c.name} = {c.value!r} on "
                              f"{c.task_id} (commit {c.commit[:10]}) {how}"[:NOTE_CHARS], c.id))
        elif ev.type == "acceptance_set":
            task = view.tasks.get(p["task_id"])
            for m in {task.assignee, task.created_by} - {p["by"], None} if task else ():
                notes.append((m, f"@{p['by']} set the acceptance spec of {task.id} "
                              f"({p['spec_sha']}); see acceptance(task='{task.short}')",
                              p["spec_sha"]))
        elif ev.type == "adjudication_requested":
            task = view.tasks.get(p["task_id"])
            c = inst.claims.get(p["claim_id"])
            judges = [m for m in view.members
                      if self.svc.selector_holds("role:adjudicator", m, m, task)] or \
                [h.id for h in view.humans()]
            att = inst.attestations.get(p["attestation_id"])
            for m in judges:
                notes.append((m, f"Adjudicate claim {p['name']} ({p['claim_id']}) of "
                              f"{p['task_id']}: @{p['by']} — {p['reason']}; basis: "
                              f"{att.basis if att else '-'}. The author's value is "
                              f"{c.value:g} on commit {c.commit[:10]}. Reproduce it from the "
                              f"repository yourself, then attest(task='{p['task_id']}', "
                              f"claim='{p['claim_id']}', verdict=pass|reject, basis=..., "
                              f"counterexample=<your replayed claim, for reject>)"[:NOTE_CHARS]
                              if c else f"Adjudicate claim {p['claim_id']} of {p['task_id']}",
                              p["claim_id"]))
        elif ev.type == "attestation_recorded" and p["verdict"] == "pass":
            task = view.tasks.get(p["task_id"])
            if task is not None and not inst.unattested(task.id, None):
                deciders = {task.reviewer} | {
                    m for m in view.members
                    if self.svc.selector_holds("role:adjudicator", m, m, task)}
                for m in deciders - {None}:
                    notes.append((m, f"Every claim of {task.id} now has a pass attestation; "
                                  f"approve it with task_transition(task='{task.short}', "
                                  f"transition=approve) if you are its decider", p["attestation_id"]))
        elif ev.type == "defect_recorded":
            task = view.tasks.get(p["task_id"])
            leads = {m for m in view.members if self.svc.selector_holds("role:lead", m, m, task)}
            dec = p.get("decision") or {}
            spec = self.svc.inst.rules.constitution.amendment if \
                self.svc.inst.rules.constitution else None
            authors = ({task.assignee} if task else set()) - {p["by"], None}
            for m in leads | authors:
                how = (f" The rules it passed: {', '.join(dec.get('rules', [])) or '-'}. If a "
                       f"rule should have caught it, propose_amendment(changes=[...], defects="
                       f"['{p['defect_id']}'], rationale=...); see `defects`."
                       if m in leads and spec is not None else "")
                notes.append((m, f"Defect {p['defect_id']} [{p['source']}] on {p['task_id']}"
                              + (f" claim {p['claim_id']}" if p.get("claim_id") else "")
                              + f" by @{p['by']}: {p['reason']}.{how}"[:NOTE_CHARS],
                              p["defect_id"]))
        elif ev.type in ("amendment_enacted", "amendment_closed"):
            a = inst.amendments.get(p["amendment_id"])
            if a is not None:
                how = "enacted: the new rules are in force" if ev.type == "amendment_enacted" \
                    else f"closed ({p['reason']}{': ' + p['note'] if p.get('note') else ''})"
                notes.append((a.proposer, f"Your amendment {a.id} to {a.path} was {how}"
                              [:NOTE_CHARS], a.id))
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
        return f"{decision.rstrip('e')}ed {id}" if r.ok else f"denied: {r.decision.reason}"
    finally:
        tr.close()
