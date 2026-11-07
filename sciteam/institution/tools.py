"""Institution tools and the texts members see (docs/40-institution.md §11). Tools reach the
service through ctx.extras["institution"]; Acts go through the same CommClient.submit."""

from typing import Any

import yaml

from sciteam.comm.service import CommClient
from sciteam.comm.tools import _obj, _result
from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.institution.amend import CHANGES_HELP
from sciteam.institution.defects import render_defects
from sciteam.institution.gate import act_task, summarize
from sciteam.institution.model import VOCABULARY
from sciteam.institution.service import InstitutionService
from sciteam.providers.llm.types import ToolSpec

_STR = {"type": "string"}


def _svc(ctx: ToolContext) -> InstitutionService:
    return ctx.extras["institution"]


def _client(ctx: ToolContext) -> CommClient:
    return ctx.extras["comm"]


# ---- texts ------------------------------------------------------------------------------

def render_procedure(svc: InstitutionService) -> str:
    rs = svc.inst.rules
    spec = rs.constitution.amendment if rs.constitution else None
    if spec is None:
        return "The rules cannot be amended during this run."
    p = spec.pass_
    need = "every voter" if p.yes_at_least == "all" else f"at least {p.yes_at_least}"
    return (f"Amendable files: {', '.join(spec.amendable)}. Proposers: {spec.proposers}; "
            f"voters: {spec.voters}. An amendment passes with {need} yes and at most "
            f"{p.no_at_most} no within {spec.voting_window_s / 60:.0f} minutes"
            f"{_ratify_text(spec)}. Protected rules (must stay unchanged; rule:key keeps "
            f"that rule's conditions with the key): {', '.join(spec.protected_rules) or 'none'}."
            f" Proposing counts as your yes vote if you are a voter."
            + (f" Admission of a change to the rules file: the proposal cites defect records and re-adjudicating the recorded "
               f"Acts must fix >= {spec.admission.fixed_at_least} of the Acts they concern and "
               f"regress at most max({spec.admission.regressed_at_most}, "
               f"{spec.admission.regressed_fraction:.0%} of allowed Acts); a validator the new "
               f"rules run is first run on the recorded commits it would have checked, and "
               f"admission waits for those runs; a proposal that would allow Acts once denied "
               f"needs human ratification." if spec.admission else ""))


def _ratify_text(spec) -> str:
    if spec.human_ratify is True:
        return ", then human ratification"
    if spec.human_ratify:
        return f", then human ratification for changes to {', '.join(spec.human_ratify)}"
    return ""


def render_record(svc: InstitutionService, member: str, limit: int = 8) -> str:
    inst = svc.inst
    mine = [e for e in inst.ledger if e["member"] == member]
    lines = [f"Your balance: {inst.totals.get(member, 0):+g} ({len(mine)} entries)"]
    lines += [f"- {e['amount']:+g} {e['rule']}" + (f" ({e['task']})" if e.get("task") else "")
              for e in mine[-limit:]]
    return "\n".join(lines)


def awaiting(svc: InstitutionService, member: str) -> list[str]:
    """Approvals this member may decide and amendments this member may still vote on."""
    inst, out = svc.inst, []
    for st in inst.approvals.values():
        if st.status != "requested":
            continue
        subject = inst.acts.get(st.key)
        task = act_task(subject, svc.comm.view) if subject else None
        if svc.selector_holds(st.approver, member, member, task):
            out.append(f"- decide_pending(id='{st.key}'): {st.summary}")
    spec = inst.rules.constitution.amendment if inst.rules and inst.rules.constitution else None
    for a in inst.amendments.values():
        if a.status == "open" and spec and member in svc.voters(a) and member not in a.votes:
            t = svc.tally(a)
            out.append(f"- vote(amendment='{a.id}') on @{a.proposer}'s change to {a.path} "
                       f"({t['yes']}/{t['yes_needed']} yes so far): {a.rationale[:300]}")
    return out


def render_waiting_mine(svc: InstitutionService, member: str) -> list[str]:
    out = []
    for aid, p in svc.inst.pending.items():
        if p.act.actor == member:
            out.append(f"- {aid}: {summarize(p.act)} — waiting for {', '.join(p.waiting)}")
    return out


def render_certificates(svc: InstitutionService) -> str:
    rs, inst = svc.inst.rules, svc.inst
    lines = []
    for env, spec in sorted(rs.rules.environments.items()):
        ok, why = inst.certificate(env)
        lines.append(f"- {env} (probes {', '.join(spec.probes)}): "
                     f"{'IN FORCE — ' if ok else 'not in force — '}{why}")
    return "\n".join(lines) or "(none defined)"


# ---- tools ------------------------------------------------------------------------------

class RulesTool:
    readonly = True
    spec = ToolSpec(
        name="rules",
        description=("Show the institution in force: its version, principles, the rules file "
                     "and how rules are amended. file: show the full text of one rule-set file "
                     "(e.g. the communication policy) instead."),
        parameters=_obj({"file": _STR}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rs = _svc(ctx).inst.rules
        if args.get("file"):
            text = rs.texts.get(str(args["file"]))
            if text is None:
                return ToolResult(content=f"error: no rule-set file '{args['file']}'; files: "
                                  f"{', '.join(rs.texts)}", is_error=True)
            return ToolResult(content=f"# {args['file']} (sha {rs.files[args['file']][:12]})\n"
                              f"{text}")
        parts = [f"Rule version {rs.version}; files: {', '.join(rs.texts)}"]
        if rs.constitution:
            parts += ["## Principles", rs.constitution.principles.strip() or "(none)"]
        if rs.rules_path:
            parts += [f"## Rules ({rs.rules_path})", rs.texts[rs.rules_path].strip() or "(empty)"]
        if rs.rules.environments:
            parts += ["## Environment certificates", render_certificates(_svc(ctx))]
        parts += ["## Amendment", render_procedure(_svc(ctx)), "## Rule vocabulary", VOCABULARY]
        return ToolResult(content="\n\n".join(parts))


class ProposeAmendmentTool:
    readonly = False
    spec = ToolSpec(
        name="propose_amendment",
        description=("Propose a change to the rules. Prefer changes=[...], structured edits of "
                     "the rules file in force; or give path and content, the complete new text "
                     "of an amendable file (see rules(file=...)). defects: the defect records "
                     "(see `defects`) the change fixes. The institution re-decides every "
                     "recorded Act of the project under the proposed rules and admits the "
                     "proposal only if it fixes the cited defects without regressing accepted "
                     "work. rationale: why.\n" + CHANGES_HELP),
        parameters=_obj({"changes": {"type": "array", "items": {"type": "object"}},
                         "defects": {"type": "array", "items": _STR},
                         "path": _STR, "content": _STR, "rationale": _STR}, ["rationale"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("propose_amendment", args)
        review = r.data.get("review") or ""
        return _result(r, f"proposed {r.data.get('amendment')}:\n{review}\n"
                          f"{r.data.get('diff', '')}".replace("\n\n", "\n"))


class DefectsTool:
    readonly = True
    spec = ToolSpec(
        name="defects",
        description=("Defect records: accepted results later shown wrong (overturned by the "
                     "owner, failed replication, contradicted by a later claim), each with the "
                     "rules and endorsers of the original decision. Cite them in "
                     "propose_amendment(defects=[...])."),
        parameters=_obj({}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=render_defects(_svc(ctx).inst))


class OverturnClaimTool:
    readonly = False
    spec = ToolSpec(
        name="overturn_claim",
        description=("Record that an accepted result is wrong because a later claim that "
                     "replayed contradicts it. task: the accepted task; claim: its claim (name "
                     "or id); by_claim: the id of the later replayed claim; reason: the "
                     "contradiction. The task reopens and a defect record is written."),
        parameters=_obj({"task": _STR, "claim": _STR, "by_claim": _STR, "reason": _STR},
                        ["task", "by_claim", "reason"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("overturn_claim", args)
        return _result(r, f"recorded {r.data.get('defect')}; {r.data.get('task')} reopened")


class VoteTool:
    readonly = False
    spec = ToolSpec(
        name="vote",
        description="Vote on an open amendment. choice: yes or no. You may change your vote.",
        parameters=_obj({"amendment": _STR, "choice": {"type": "string", "enum": ["yes", "no"]},
                         "note": _STR}, ["amendment", "choice"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("vote", args)
        if not r.ok:
            return _result(r, "")
        svc = _svc(ctx)
        svc.settle()
        a = svc.inst.amendments[r.data["amendment"]]
        t = svc.tally(a)
        return ToolResult(content=f"voted {args['choice']} on {a.id}; now {a.status} "
                          f"({t['yes']} yes, {t['no']} no of {t['voters']} voters)",
                          meta={"act_id": r.act_id, **r.data})


class WithdrawAmendmentTool:
    readonly = False
    spec = ToolSpec(name="withdraw_amendment",
                    description="Withdraw an amendment you proposed.",
                    parameters=_obj({"amendment": _STR}, ["amendment"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("withdraw_amendment", args)
        return _result(r, f"withdrew {r.data.get('amendment')}")


class DecidePendingTool:
    readonly = False
    spec = ToolSpec(
        name="decide_pending",
        description=("Approve or reject something that awaits your approval (see `pending`). "
                     "id: the act or amendment id. A rejection needs a note."),
        parameters=_obj({"id": _STR, "decision": {"type": "string",
                                                  "enum": ["approve", "reject"]},
                         "note": _STR}, ["id", "decision"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("decide_pending", args)
        if r.ok:
            _svc(ctx).settle()
        return _result(r, f"{str(args['decision']).rstrip('e')}ed {r.data.get('id')}")


class StandingTool:
    readonly = True
    spec = ToolSpec(
        name="standing",
        description=("The ledger: your entries and everyone's balance, and the institutional "
                     "facts established so far (e.g. which tasks count as accepted)."),
        parameters=_obj({}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc, me = _svc(ctx), _client(ctx).actor
        inst = svc.inst
        balances = ", ".join(f"@{m} {v:+g}" for m, v in sorted(inst.totals.items())) or "(none)"
        facts: dict[str, list[str]] = {}
        for fact, subject in inst.facts:
            facts.setdefault(fact, []).append(subject)
        lines = [render_record(svc, me, limit=50), "", f"Balances: {balances}", "",
                 "Facts:", *(f"- {f}: {', '.join(sorted(s))}" for f, s in sorted(facts.items()))]
        if not facts:
            lines.append("(none)")
        return ToolResult(content="\n".join(lines))


class PendingTool:
    readonly = True
    spec = ToolSpec(
        name="pending",
        description=("What is waiting: your own Acts waiting for validation or approval, what "
                     "awaits your decision or vote, and every open amendment."),
        parameters=_obj({}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc, me = _svc(ctx), _client(ctx).actor
        inst = svc.inst
        mine = render_waiting_mine(svc, me)
        for aid in inst.pending:
            for v in inst.validations_for(aid).values():
                if inst.pending[aid].act.actor == me:
                    mine.append(f"  - {v.id} {v.validator}: {v.status}")
        amendments = [f"- {a.id} by @{a.proposer} on {a.path}: {a.status}; votes "
                      f"{yaml.safe_dump(a.votes, default_flow_style=True).strip()}; "
                      f"{a.rationale[:200]}"
                      for a in inst.amendments.values() if a.status in ("trial", "open", "ratifying")]
        parts = ["## Yours, waiting", "\n".join(mine) or "(none)",
                 "## Awaiting you", "\n".join(awaiting(svc, me)) or "(none)",
                 "## Open amendments", "\n".join(amendments) or "(none)"]
        return ToolResult(content="\n".join(parts))


class ValidationOutputTool:
    readonly = True
    spec = ToolSpec(
        name="validation_output",
        description=("Full output of a validation run (id e.g. 'V3'), paged by 0-based line "
                     "offset and limit."),
        parameters=_obj({"id": _STR, "offset": {"type": "integer"},
                         "limit": {"type": "integer"}}, ["id"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        v = _svc(ctx).inst.validations.get(str(args["id"]).strip().upper())
        if v is None:
            return ToolResult(content=f"error: unknown validation '{args['id']}'", is_error=True)
        head = (f"{v.id} {v.validator} on {v.commit[:10]}: {v.status}"
                f"{f' (exit {v.exit_code})' if v.exit_code is not None else ''}; "
                f"command: {v.command}")
        if not v.output_blob:
            return ToolResult(content=f"{head}\n{v.tail or '(no output)'}")
        offset = max(int(args.get("offset") or 0), 0)
        limit = max(int(args.get("limit") or 400), 1)
        total = ctx.blobs.line_count(v.output_blob)
        text = ctx.blobs.read_slice(v.output_blob, offset=offset, limit=limit)
        return ToolResult(content=f"{head}\n[lines {offset}-{min(offset + limit, total) - 1} "
                          f"of {total}]\n{text}")


class CertifyEnvironmentTool:
    readonly = False
    spec = ToolSpec(
        name="certify_environment",
        description=("Issue an environment certificate: runs the environment's probe "
                     "validators (see `rules`) on commit in a clean copy without network; the "
                     "certificate is in force only if all pass. You are notified when done."),
        parameters=_obj({"env": _STR, "commit": _STR, "note": _STR}, ["env", "commit"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("certify_environment", args)
        return _result(r, f"certified {r.data.get('env')}")


class RevokeEnvironmentTool:
    readonly = False
    spec = ToolSpec(name="revoke_environment",
                    description="Revoke an environment certificate, giving the reason.",
                    parameters=_obj({"env": _STR, "reason": _STR}, ["env", "reason"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("revoke_environment", args)
        return _result(r, f"revoked {r.data.get('env')}")


_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "reproduce": {"type": "string", "description": "shell command, run at the repository "
                      "root of a clean export, that regenerates the output files from the code"},
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "name": _STR, "file": _STR, "pointer": _STR, "description": _STR,
            "min": {"type": "number"}, "max": {"type": "number"}},
            "required": ["name", "file", "pointer"]}},
        "deliverables": {"type": "array", "items": _STR},
        "rel_tol": {"type": "number"}, "abs_tol": {"type": "number"},
        "timeout_s": {"type": "number"}, "notes": _STR},
    "required": ["reproduce", "claims"],
}


def render_acceptance(svc: InstitutionService, task_id: str, viewer: str | None = None) -> str:
    """The spec, the author's claims, and the viewer's own reviewer claims and attestations;
    the author sees reviewers' work only as counterexamples routed to them."""
    from sciteam.institution.claims import render_spec

    inst = svc.inst
    spec, cur = inst.spec(task_id), inst.specs.get(task_id)
    if spec is None:
        return f"{task_id} has no typed acceptance spec."
    lines = [f"{task_id} acceptance spec {cur['spec_sha']} (by @{cur['by']}):", render_spec(spec)]
    mine = [c for c in inst.claims.values() if c.task_id == task_id
            and c.spec_sha == cur["spec_sha"] and (c.adhoc is None or c.actor == viewer)]
    if mine:
        lines.append("Claims:")
        lines += [f"- {c.id} {c.name} = {c.value:g} on {c.commit[:10]} by @{c.actor}"
                  f"{' (own reproduction: ' + c.adhoc['reproduce'] + ')' if c.adhoc else ''}: "
                  f"{c.status}{' (' + c.code + ')' if c.code else ''}" for c in mine[-30:]]
    atts = [a for a in inst.attestations.values() if a.task_id == task_id and a.by == viewer]
    if atts:
        lines.append("Your attestations:")
        lines += [f"- {a.id} {a.name} ({a.claim_id}): {a.verdict}"
                  f"{' → ' + a.routed if a.routed else ''}" for a in atts[-30:]]
    return "\n".join(lines)


class ProposeAcceptanceTool:
    readonly = False
    spec = ToolSpec(
        name="propose_acceptance",
        description=("Set the typed acceptance spec of a task you created or are assigned: the "
                     "command that reproduces its outputs and the numbers (claims) it must "
                     "report, each at file + JSON Pointer (e.g. results.json, /fit/Km), with an "
                     "optional plausible range [min, max]. Where the rules ask for it, the spec "
                     "waits for the owner's confirmation. Submitting the task then needs each "
                     "claim recorded with record_claim and replayed from a clean export."),
        parameters=_obj({"task": _STR, "spec": _SPEC_SCHEMA}, ["task", "spec"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("propose_acceptance", args)
        res = _result(r, f"acceptance spec {r.data.get('spec_sha')} set on {r.data.get('task')}")
        if res.is_error:
            return res
        preview = await _preview(args.get("spec"), ctx)
        if preview:
            res.content += ("\nThe workspace's current files, read at each claim's pointer:\n"
                            + "\n".join(f"- {p}" for p in preview))
        return res


async def _preview(raw: Any, ctx: ToolContext) -> list[str]:
    from sciteam.institution.claims import parse_spec, preview_spec

    try:
        spec = parse_spec(raw)
    except Exception:
        return []
    texts: dict[str, str | None] = {}
    for f in {c.file for c in spec.claims}:
        try:
            texts[f] = (await ctx.sandbox.read_file(f)).decode()
        except Exception:
            texts[f] = None
    return preview_spec(spec, texts)


class RecordClaimTool:
    readonly = False
    spec = ToolSpec(
        name="record_claim",
        description=("Record the value of one claim the task's acceptance spec names, as "
                     "computed on commit. The spec's reproduce command reruns on a clean export "
                     "of that commit without network; the claim replays when the value read at "
                     "its file + pointer matches within tolerance (and lies in its range). You "
                     "are notified of the outcome. A reviewer records their own reproduction "
                     "the same way, with reproduce=<their command, e.g. python "
                     "/private/<you>/check.py> and, for a quantity the spec does not name, "
                     "file and pointer; such a claim is the basis or counterexample of attest."),
        parameters=_obj({"task": _STR, "claim": _STR, "value": {"type": "number"},
                         "commit": _STR, "reproduce": _STR, "file": _STR, "pointer": _STR},
                        ["task", "claim", "value", "commit"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("record_claim", args)
        return _result(r, f"recorded {r.data.get('claim')} on {r.data.get('task')}; its replay "
                       f"runs now and you are notified of the outcome")


class AttestTool:
    readonly = False
    spec = ToolSpec(
        name="attest",
        description=("Attest one claim of a task in review, after reproducing it yourself. "
                     "verdict=pass with basis=<id of your claim on this task that replayed>; "
                     "verdict=reject with counterexample=<id of a claim you recorded whose "
                     "replay shows the defect> — the author then receives only that "
                     "counterexample; verdict=abstain when you disagree without a "
                     "counterexample — the claim goes to adjudication."),
        parameters=_obj({"task": _STR, "claim": _STR,
                         "verdict": {"type": "string", "enum": ["pass", "reject", "abstain"]},
                         "basis": _STR, "counterexample": _STR},
                        ["task", "claim", "verdict", "basis"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("attest", args)
        routed = {"author": "; the task went back to its author with the counterexample",
                  "adjudication": "; the claim went to adjudication"}.get(
            r.data.get("routed", ""), "")
        return _result(r, f"attested ({r.data.get('attestation')}) on {r.data.get('task')}"
                       f"{routed}")


class AcceptanceTool:
    readonly = True
    spec = ToolSpec(
        name="acceptance",
        description="Show a task's typed acceptance spec and the claims recorded against it.",
        parameters=_obj({"task": _STR}, ["task"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        task = svc.comm.view.resolve_task(str(args.get("task", "")))
        if task is None:
            return ToolResult(content=f"error: unknown task '{args.get('task')}'", is_error=True)
        return ToolResult(content=render_acceptance(svc, task.id, _client(ctx).actor))


INSTITUTION_TOOLS = {t.spec.name: t for t in (
    ProposeAcceptanceTool(), RecordClaimTool(), AttestTool(), AcceptanceTool(),
    CertifyEnvironmentTool(), RevokeEnvironmentTool(),
    RulesTool(), ProposeAmendmentTool(), VoteTool(), WithdrawAmendmentTool(),
    DecidePendingTool(), StandingTool(), PendingTool(), ValidationOutputTool(),
    DefectsTool(), OverturnClaimTool())}
