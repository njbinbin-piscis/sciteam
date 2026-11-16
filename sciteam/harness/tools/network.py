"""`request_network`: a member asks the owner to open one more domain for the sandboxes.

The request is written to `<run>/net_requests.json` and waits there; the product shows it to the
owner. Once approved, the domain is in `<run>/net_allow.json`, which the egress proxy reads on
every request, so it takes effect at once without restarting any sandbox."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.types import ToolSpec

REQUESTS, ALLOW = "net_requests.json", "net_allow.json"
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def approved_domains(run_dir: Path) -> list[str]:
    return [str(d) for d in read_json(Path(run_dir) / ALLOW, [])]


class RequestNetworkTool:
    readonly = False
    spec = ToolSpec(
        name="request_network",
        description=(
            "Ask the owner to allow the sandboxes to reach one more domain (for example to install "
            "a package or download a model). Give the exact domain and why it is needed. The "
            "request waits for the owner; ask again later or carry on with what does not need it. "
            "Only exact domains are accepted, no wildcards."),
        parameters={"type": "object", "properties": {
            "domain": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["domain", "reason"], "additionalProperties": False},
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        run_dir = ctx.extras.get("run_dir")
        if run_dir is None:
            return ToolResult(content="error: network requests are not available here", is_error=True)
        egress = ctx.extras.get("egress")
        if not (egress() if callable(egress) else egress):
            return ToolResult(content="error: this project's sandboxes have no network at all, so a "
                              "domain cannot be added; tell the owner what you need and why",
                              is_error=True)
        domain = str(args["domain"]).strip().lower()
        if not DOMAIN.match(domain):
            return ToolResult(content=f"error: {domain!r} is not a plain domain name", is_error=True)
        run_dir = Path(run_dir)
        if domain in approved_domains(run_dir):
            return ToolResult(content=f"{domain} is already allowed")
        reqs = read_json(run_dir / REQUESTS, [])
        for r in reqs:
            if r["domain"] == domain and r["status"] == "pending":
                return ToolResult(content=f"request {r['id']} for {domain} is waiting for the owner")
            if r["domain"] == domain and r["status"] == "denied":
                return ToolResult(content=f"the owner denied {domain}; do not ask again unless the "
                                  "need changed, and say what changed", is_error=True)
        rid = new_id("net")[-8:]
        reqs.append({"id": rid, "domain": domain, "reason": str(args["reason"])[:500],
                     "by": ctx.extras.get("agent", ""), "status": "pending", "at": time.time()})
        (run_dir / REQUESTS).write_text(json.dumps(reqs, ensure_ascii=False, indent=1))
        return ToolResult(content=f"request {rid} for {domain} sent to the owner; it takes effect as "
                          "soon as they approve", meta={"request": rid})


NETWORK_TOOLS = {"request_network": RequestNetworkTool()}
