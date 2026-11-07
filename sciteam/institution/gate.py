"""InstitutionGate: requirements, approvals and pending decisions (docs/40-institution.md §6).
A pure function of the Act and the folded state; resolving a pending Act is deciding again."""

from collections.abc import Callable

from sciteam.comm.model import Act, Decision, Task, deny
from sciteam.comm.view import CommView
from sciteam.institution.facts import attester, children_lacking, member_of, reads_private_of
from sciteam.institution.model import ActMatch, Condition
from sciteam.institution.view import INST_STREAM, InstitutionView

ResolveCommit = Callable[[str], str | None]   # git revision -> full sha, None if unknown
ACCEPTANCE = "{task.acceptance}"
HELD = ("assigned", "in_progress", "blocked")


def _alt(pattern: str | None, value: str | None) -> bool:
    return pattern is None or (value is not None and value in pattern.split("|"))


def act_task(act: Act, view: CommView) -> Task | None:
    if act.kind in ("task_transition", "task_update", "propose_acceptance", "record_claim",
                    "attest", "overturn", "overturn_claim", "goal_verdict"):
        return view.resolve_task(str(act.params.get("task", "")))
    return None


def matches(m: ActMatch, act: Act, view: CommView) -> bool:
    if m.act != act.kind or not _alt(m.transition, act.params.get("transition")):
        return False
    if m.where is None:
        return True
    task = act_task(act, view)
    labels = list(act.params.get("labels") or []) if act.kind == "task_create" else \
        (task.labels if task else [])
    parent = act.params.get("parent") if act.kind == "task_create" else \
        (task.parent if task else None)
    if m.where.task_label is not None and m.where.task_label not in labels:
        return False
    return m.where.task_is_root is None or m.where.task_is_root == (parent is None)


class InstitutionGate:
    def __init__(self, inst: InstitutionView, resolve_commit: ResolveCommit,
                 uncommitted_named: Callable[[str, str], list[str]] | None = None,
                 committed_named: Callable[[str, str], list[str]] | None = None,
                 private_base: str | None = None):
        self.inst = inst
        self.private_base = private_base   # where members' private directories are mounted
        self.resolve_commit = resolve_commit
        # (commit, command) -> workspace files the command names that the commit lacks / holds
        self.uncommitted_named = uncommitted_named or (lambda sha, command: [])
        self.committed_named = committed_named or (lambda sha, command: [])

    def rule_version(self) -> str | None:
        return self.inst.rules.version if self.inst.rules else None

    # ---- decide -------------------------------------------------------------------------
    def decide(self, act: Act, view: CommView) -> Decision:
        rs = self.inst.rules
        if rs is None:
            return Decision(decision="allow")
        task = act_task(act, view)
        waiting: list[str] = []
        if act.kind == "certify_environment":
            env = rs.rules.environments.get(str(act.params.get("env", "")))
            if env is None:
                return deny(f"unknown environment '{act.params.get('env')}'; defined: "
                            f"{sorted(rs.rules.environments) or 'none'}", "environments")
            d = self._condition(Condition(validate=env.probes), act, task, view, waiting)
            if d is not None:
                return deny(d, "environments")
        for req in rs.rules.requirements:
            if not matches(req, act, view):
                continue
            for cond in req.require:
                d = self._condition(cond, act, task, view, waiting)
                if d is not None:
                    return deny(d, req.name)
        if waiting:
            vals = [w.split(":", 1)[1] for w in waiting if w.startswith("validate:")]
            reps = [w.split(":", 1)[1] for w in waiting if w.startswith("claim:")]
            what = "; ".join(filter(None, [
                f"validators {', '.join(vals)}" if vals else "",
                f"claim replays {', '.join(reps)}" if reps else ""]))
            return self._pending(act, task, view, Decision(
                decision="pending", reason=f"waiting for {what}", waiting=waiting))
        for ap in rs.rules.approvals:
            if not matches(ap, act, view):
                continue
            state = self.inst.approvals.get(act.act_id)
            if state is None or state.status == "requested":
                return self._pending(act, task, view, Decision(
                    decision="pending", rule=ap.name,
                    reason=f"waiting for approval by {ap.approver} ({ap.name})",
                    waiting=[f"approval:{ap.name}"]))
            if state.status == "rejected":
                return deny(f"rejected by @{state.by}: {state.note}", ap.name)
        return Decision(decision="allow")

    def _pending(self, act: Act, task: Task | None, view: CommView, d: Decision) -> Decision:
        """At most one waiting transition per task; Acts that need no wait still go through."""
        if act.kind != "task_transition" or task is None:
            return d
        other = next((p for aid, p in self.inst.pending.items() if aid != act.act_id and
                      p.act.kind == "task_transition" and act_task(p.act, view) is task), None)
        if other is None:
            return d
        return deny(f"{task.id} already has a pending {other.act.params.get('transition')} "
                    f"({other.act.act_id}) waiting for {', '.join(other.waiting)}", "pending")

    def _condition(self, c: Condition, act: Act, task: Task | None, view: CommView,
                   waiting: list[str]) -> str | None:
        """None if satisfied (or now waiting), else the reason for denial."""
        k = c.key
        if k == "field":
            given = act.params.get(c.field)
            if act.kind == "task_create" or (act.kind == "task_update" and given is not None):
                value = given
            else:
                value = getattr(task, c.field, None) if task else None
            return None if value not in (None, "", []) else \
                f"the task needs {'an' if c.field[0] in 'aeiou' else 'a'} {c.field}"
        if k == "evidence":
            return self._evidence(act)
        if k == "validate":
            # 验证总是跑在 Act 的 evidence commit 上，因此 validate 蕴含 evidence
            missing = self._evidence(act)
            if missing is not None:
                return missing
            done = self.inst.validations_for(act.act_id)
            for v in c.validate_ or []:
                spec = self.inst.rules.rules.validators[v]
                if spec.command == ACCEPTANCE and not (task and task.acceptance):
                    return f"{task.id if task else 'the task'} has no acceptance command"
                rec = done.get(v)
                if rec is None or rec.status == "requested":
                    waiting.append(f"validate:{v}")
                elif rec.status == "failed":
                    full = f"; full output: validation_output(id='{rec.id}')" \
                        if rec.output_blob else ""
                    owner = ""
                    if spec.command == ACCEPTANCE and task:
                        owner = (f"; it runs {task.id}'s acceptance command, written by "
                                 f"@{task.created_by}. When the command itself is wrong, tell "
                                 f"@{task.created_by}, who can change it with task_update; a "
                                 f"resubmit of the same commit gives the same result")
                    return (f"validator {v} ({rec.id}) failed on commit {(rec.commit or '?')[:10]} "
                            f"(exit {rec.exit_code}){full}{owner}; output ends with:\n{rec.tail}")
            return None
        if k == "holds_at_most":
            held = [t.id for t in view.tasks.values() if t.assignee == act.actor and
                    t.status in HELD and (task is None or t.id != task.id)]
            return None if len(held) <= c.holds_at_most else (
                f"@{act.actor} already holds {', '.join(held)}; submit or hand back "
                f"{'one' if c.holds_at_most else 'it'} before taking another "
                f"(at most {c.holds_at_most} other unfinished task"
                f"{'' if c.holds_at_most == 1 else 's'})")
        if k == "distinct":
            a, b = (member_of(r, task, act.actor) for r in c.distinct)
            return None if a is None or b is None or a != b else \
                f"{c.distinct[0]} and {c.distinct[1]} must be different members (@{a})"
        if k == "children_have":
            lacking = children_lacking(task, c.children_have, view, self.inst) if task else []
            return None if not lacking else \
                f"subtasks without '{c.children_have}': {', '.join(lacking)}"
        if k == "certificate":
            ok, why = self.inst.certificate(c.certificate)
            return None if ok else why
        if k in ("spec_typed", "claims_cover", "claims_replayed"):
            return self._claims(k, act, task, waiting)
        if k == "claims_attested":
            if task is None:
                return "claims_attested applies to Acts on a task"
            lacking = self.inst.unattested(task.id, attester(c.claims_attested, task, view))
            return None if not lacking else (
                f"claims of {task.id} not attested by {c.claims_attested}: "
                f"{', '.join(lacking)}; each needs a pass from attest(task, claim, verdict=pass, "
                f"basis=...)")
        if k == "independent_of":
            author = task.assignee if task else None
            if author is None:
                return "independent_of applies to Acts on an assigned task"
            if act.actor == author:
                return f"@{act.actor} is the author of {task.id} and cannot attest its claims"
            if author in reads_private_of(view, act.actor):
                return (f"@{act.actor} can read @{author}'s private directory, so cannot "
                        f"attest {task.id} independently")
            return None
        return f"condition '{k}' only applies to facts"

    def _claims(self, k: str, act: Act, task: Task | None, waiting: list[str]) -> str | None:
        if task is None:
            return f"{k} applies to Acts on a task"
        spec = self.inst.spec(task.id)
        if spec is None:
            return (f"{task.id} has no typed acceptance spec; @{task.created_by} or "
                    f"@{task.assignee} proposes one with propose_acceptance")
        if k == "spec_typed":
            return None
        missing = self._evidence(act)
        if missing is not None:
            return missing
        sha = self.resolve_commit(str(act.params.get("commit")))
        on = self.inst.claims_on(task.id, sha)
        absent = [c.name for c in spec.claims if c.name not in on]
        if absent:
            return (f"claims not recorded for {task.id} on commit {sha[:10]}: "
                    f"{', '.join(absent)} — record each with record_claim(task, claim, value, "
                    f"commit)" + self._elsewhere(task, sha))
        if k == "claims_cover":
            return None
        for c in spec.claims:
            rec = on[c.name]
            if rec.status == "requested":
                waiting.append(f"claim:{rec.id}")
            elif rec.status == "failed":
                seen = f", reproduction gave {rec.observed!r}" if rec.observed is not None else ""
                return (f"claim {c.name} ({rec.id}) = {rec.value!r} did not replay on commit "
                        f"{sha[:10]}: {rec.code}{seen}; output ends with:\n{rec.tail[-1500:]}"
                        + self._elsewhere(task, sha))
        return None

    def _elsewhere(self, task: Task, sha: str) -> str:
        """Where the author's claims stand on their newest other commit, and where HEAD is."""
        head = self.resolve_commit("HEAD")
        s = self.inst.specs.get(task.id) or {}
        mine = [c for c in self.inst.claims.values() if c.task_id == task.id and
                c.adhoc is None and c.commit != sha and c.spec_sha == s.get("spec_sha")]
        out = f"\n[HEAD is {head[:10]}]" if head and head != sha else ""
        if mine:
            other = max(mine, key=lambda c: c.seq).commit
            on = self.inst.claims_on(task.id, other)
            states = ", ".join(f"{n} {x.status}" + (f" ({x.code})" if x.code and x.code != "ok"
                                                    else "") for n, x in sorted(on.items()))
            out += f"\n[your newest claims are on commit {other[:10]}: {states}]"
        return out

    def _evidence(self, act: Act) -> str | None:
        ref = str(act.params.get("commit") or "").strip()
        if not ref:
            return ("give commit=<git revision in /workspace that holds the deliverable>; "
                    "commit your work first")
        return None if self.resolve_commit(ref) else \
            f"'{ref}' is not a commit in the /workspace repository"

    # ---- requests written with a pending decision ----------------------------------------
    def on_pending(self, act: Act, decision: Decision,
                   view: CommView) -> list[tuple[str, str, dict]]:
        out: list[tuple[str, str, dict]] = []
        rs = self.inst.rules
        done = self.inst.validations_for(act.act_id)
        n = len(self.inst.validations)
        for w in decision.waiting:
            kind, name = w.split(":", 1)
            if kind == "validate" and name not in done:
                spec = rs.rules.validators[name]
                task = act_task(act, view)
                command = spec.command.replace(ACCEPTANCE, task.acceptance or "") if task \
                    else spec.command
                n += 1
                out.append((INST_STREAM, "validation_requested", {
                    "validation_id": f"V{n}", "validator": name, "act_id": act.act_id,
                    "commit": self.resolve_commit(str(act.params.get("commit"))),
                    "command": command, "timeout_s": spec.timeout_s}))
            elif kind == "approval" and act.act_id not in self.inst.approvals:
                ap = next(a for a in rs.rules.approvals if a.name == name)
                out.append((INST_STREAM, "approval_requested", {
                    "key": act.act_id, "approval": name, "approver": ap.approver,
                    "actor": act.actor, "summary": summarize(act)}))
        return out


def summarize(act: Act) -> str:
    p = act.params
    if act.kind == "task_transition":
        return f"@{act.actor} wants to {p.get('transition')} {p.get('task')}" + \
            (f": {p['note']}" if p.get("note") else "")
    if act.kind == "propose_acceptance":
        from sciteam.institution.claims import parse_spec, render_spec

        try:
            body = render_spec(parse_spec(p.get("spec")))
        except Exception:
            body = str(p.get("spec"))[:1500]
        return f"@{act.actor} proposes the acceptance spec of {p.get('task')}:\n{body}"
    return f"@{act.actor} {act.kind} {p}"
