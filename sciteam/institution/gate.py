"""InstitutionGate: requirements, approvals and pending decisions (docs/40-institution.md §6).
A pure function of the Act and the folded state; resolving a pending Act is deciding again."""

from collections.abc import Callable

from sciteam.comm.model import Act, Decision, Task, deny
from sciteam.comm.view import CommView
from sciteam.institution.facts import children_lacking, member_of
from sciteam.institution.model import ActMatch, Condition
from sciteam.institution.view import INST_STREAM, InstitutionView

ResolveCommit = Callable[[str], str | None]   # git revision -> full sha, None if unknown
ACCEPTANCE = "{task.acceptance}"


def _alt(pattern: str | None, value: str | None) -> bool:
    return pattern is None or (value is not None and value in pattern.split("|"))


def act_task(act: Act, view: CommView) -> Task | None:
    if act.kind in ("task_transition", "task_update"):
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
    def __init__(self, inst: InstitutionView, resolve_commit: ResolveCommit):
        self.inst = inst
        self.resolve_commit = resolve_commit

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
            names = ", ".join(w.split(":", 1)[1] for w in waiting)
            return self._pending(act, task, view, Decision(
                decision="pending", reason=f"waiting for validators {names}", waiting=waiting))
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
                    return (f"validator {v} ({rec.id}) failed on commit {(rec.commit or '?')[:10]} "
                            f"(exit {rec.exit_code}){full}; output ends with:\n{rec.tail}")
            return None
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
        return f"condition '{k}' only applies to facts"

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
    return f"@{act.actor} {act.kind} {p}"
