"""Organization tools and the texts members see (docs/50-organization.md §9). Tools reach the
service through ctx.extras["org"]; hire and dismiss go through the same CommClient.submit."""

from typing import Any

from sciteam.comm.service import CommClient
from sciteam.comm.tools import _obj, _result
from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.kernel.config_store import split_front_matter
from sciteam.org.service import OrgService
from sciteam.providers.llm.types import ToolSpec

_STR = {"type": "string"}
ORG_TEMPLATES = "org/templates"


def _svc(ctx: ToolContext) -> OrgService:
    return ctx.extras["org"]


def _client(ctx: ToolContext) -> CommClient:
    return ctx.extras["comm"]


def render_chart(svc: OrgService) -> str:
    man = svc.manifest
    if man is None:
        return "(no organization manifest in force)"
    lines = []
    for s in man.seats:
        who = svc.seated(s.name)
        free = s.count - len(who)
        extra = [f"team {s.team}"] if s.team else []
        extra += [f"reports to {s.reports_to}"] if s.reports_to else []
        extra += [f"groups {','.join(s.groups)}"] if s.groups else []
        lines.append(f"- {s.name} ×{s.count}: {', '.join('@' + w for w in who) or 'nobody'}"
                     f"{f' — {free} vacant' if free else ''}"
                     f"{' (' + '; '.join(extra) + ')' if extra else ''}")
    return "\n".join(lines)


def render_goals(svc: OrgService) -> str:
    spec = svc.manifest.goals if svc.manifest else None
    if spec is None:
        return "(the organization declares no goals)"
    goals = svc.goals(include_cancelled=True)
    if not goals:
        return (f"No goal yet. A goal is a root task labelled '{spec.label}' with an acceptance "
                f"command (its success predicate); it is met when it has the fact "
                f"'{spec.fact}'. When every goal is met the run stops.")
    return "\n".join(
        f"- {t.id} [{t.status}] {t.title}: "
        f"{'MET' if svc.inst.has_fact(spec.fact, t.id) else 'not met'}; predicate: "
        f"{t.acceptance or '(none)'}" for t in goals)


def render_org(svc: OrgService, member: str) -> str:
    man = svc.manifest
    if man is None:
        return "(no organization manifest in force)"
    me = svc.comm.view.members.get(member)
    seat = man.seat(me.role) if me else None
    parts = []
    if man.mission.strip():
        parts += ["Mission:", man.mission.strip(), ""]
    if seat is not None:
        parts += [f"Your seat: {seat.name}" + (f" (reports to {seat.reports_to})"
                                               if seat.reports_to else ""),
                  seat.charter.strip() or "(no charter text)", ""]
    parts += ["Organization chart:", render_chart(svc), "", "Goals:", render_goals(svc)]
    return "\n".join(parts)


class OrgTool:
    readonly = True
    spec = ToolSpec(
        name="org",
        description=("The organization in force: mission, your seat and its charter, every "
                     "seat with who holds it and vacancies, and the goals with their status. "
                     "The manifest file itself: rules(file=<manifest path>)."),
        parameters=_obj({}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        return ToolResult(content=f"Manifest: {svc.path}\n\n"
                          f"{render_org(svc, _client(ctx).actor)}")


class ProfilesTool:
    readonly = True
    spec = ToolSpec(
        name="profiles",
        description=("The agent profile templates one can hire from (name, title, the seats "
                     "it fits, its first line). profile_show(name) gives the full text."),
        parameters=_obj({}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        lines = []
        for f in svc.configs.list_dir(svc.templates_dir):
            if not f.endswith(".md"):
                continue
            name = f[:-3]
            _, head, body = svc.template(name)
            first = next((ln for ln in body.splitlines() if ln.strip()), "")[:200]
            fits = ", ".join(head.get("fits", [])) or "-"
            lines.append(f"- {name}: {head.get('title', name)} (fits: {fits}) — {first}")
        return ToolResult(content="\n".join(lines) or "(no templates)")


class ProfileShowTool:
    readonly = True
    spec = ToolSpec(name="profile_show", description="Full text of a profile template.",
                    parameters=_obj({"name": _STR}, ["name"]))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from sciteam.comm.service import Reject

        try:
            text, _, _ = _svc(ctx).template(str(args["name"]).strip())
        except Reject as r:
            return ToolResult(content=f"error: {r.decision.reason}", is_error=True)
        return ToolResult(content=text)


class OrgTemplatesTool:
    readonly = True
    spec = ToolSpec(
        name="org_templates",
        description=("Organization templates (collaboration modes). Without name: the list. "
                     "With name: every file of that template (manifest, communication policy, "
                     "rules) in full, to adopt or adapt by amendment."),
        parameters=_obj({"name": _STR}, []),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cs = _svc(ctx).configs
        names = [n for n in cs.list_dir(ORG_TEMPLATES)]
        name = str(args.get("name") or "").strip()
        if not name:
            out = []
            for n in names:
                head, _ = split_front_matter(cs.load_text(f"{ORG_TEMPLATES}/{n}/README.md")) \
                    if "README.md" in cs.list_dir(f"{ORG_TEMPLATES}/{n}") else ({}, "")
                out.append(f"- {n}: {head.get('summary', '')}")
            return ToolResult(content="\n".join(out) or "(no templates)")
        if name not in names:
            return ToolResult(content=f"error: unknown template '{name}'; templates: {names}",
                              is_error=True)
        parts = [f"# {ORG_TEMPLATES}/{name}/{f}\n{cs.load_text(f'{ORG_TEMPLATES}/{name}/{f}')}"
                 for f in cs.list_dir(f"{ORG_TEMPLATES}/{name}")]
        return ToolResult(content="\n\n".join(parts))


class HireTool:
    readonly = False
    spec = ToolSpec(
        name="hire",
        description=("Fill a vacant seat with a new agent member. template: a profile template "
                     "(see `profiles`); profile: optional complete profile text adapted from "
                     "the template for this seat (omit to use the template as is); harness: "
                     "one the seat allows (default: its first). member: the new id."),
        parameters=_obj({"seat": _STR, "member": _STR, "template": _STR, "profile": _STR,
                         "harness": _STR, "note": _STR}, ["seat", "member", "template"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("hire", args)
        diff = r.data.get("diff") or "(template used as is)"
        return _result(r, f"hired @{r.data.get('member')} as {r.data.get('seat')} "
                       f"(harness {r.data.get('harness')}); profile vs template:\n{diff}")


class DismissTool:
    readonly = False
    spec = ToolSpec(
        name="dismiss",
        description=("Remove an agent member from the organization, giving the reason. Their "
                     "unfinished assigned tasks return to open."),
        parameters=_obj({"member": _STR, "reason": _STR}, ["member", "reason"]),
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = _client(ctx).submit("dismiss", args)
        return _result(r, f"dismissed @{r.data.get('member')}")


ORG_TOOLS = {t.spec.name: t for t in (
    OrgTool(), ProfilesTool(), ProfileShowTool(), OrgTemplatesTool(), HireTool(),
    DismissTool())}
