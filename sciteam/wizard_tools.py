"""Wizard-callable research tools (stdlib + lab literature adapters).

Used by the project intake wizard so it can look up papers / fetch pages
instead of only asking the operator fixed questions.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sciteam import amendment as amendment_kernel
from sciteam import fs_tools
from sciteam.ask_question import execute_ask_question
from sciteam.ask_question import tool_schema as ask_question_tool_schema
from sciteam.plugin_api import PluginRegistry
from sciteam.sandbox import LocalPythonSandbox, request_from_arguments
from sciteam.tool_registry import ToolRegistry

LAB_ROOT = Path(__file__).resolve().parents[1]
_CACHE = LAB_ROOT / "runs" / "_wizard_http_cache"

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]

WIZARD_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "literature_search",
            "description": (
                "Search scholarly literature (OpenAlex / Crossref / Europe PMC) "
                "for papers matching a query. Use when the operator names a paper, "
                "asks to reproduce a system, or needs bibliographic anchors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "General web search (Wikipedia / Bing HTML fallbacks; DuckDuckGo when reachable). "
                "Use for finding paper landing pages, blog posts, or project pages "
                "when literature_search is insufficient."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "url_fetch",
            "description": (
                "Fetch an http(s) URL and return extracted text (truncated). "
                "Use after search to read an abstract page, arXiv abs, or blog post."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "citation_audit",
            "description": (
                "Audit a lit_map / reference list: resolve DOIs/PMIDs, check title/year "
                "match, and flag claims without located spans. Use before finalizing lit_map."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lit_map": {
                        "type": "object",
                        "description": (
                            "Object with papers[]/references[]/entries[] "
                            "(title, doi, pmid, claims; entries[] also "
                            "accepts citation_key as the paper key)."
                        ),
                    }
                },
                "required": ["lit_map"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "amendment_audit",
            "description": (
                "Mechanically audit an amendment proposal (Grundnorm G7): schema "
                "validity, fail-closed layer derivation, entrenchment rejection, "
                "and motivation failure_ref resolvability. Review seats must quote "
                "this ledger instead of self-certifying legality."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal": {
                        "type": "object",
                        "description": (
                            "amendment_proposal object "
                            "(see schemas/amendment_proposal.schema.json)."
                        ),
                    }
                },
                "required": ["proposal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_sandbox",
            "description": (
                "Run a declared Python program in the mission workspace with "
                "network denied and bounded time, memory and output. Use for "
                "problem models, simulations and historical backtests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "program": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "default": 30},
                    "memory_mb": {"type": "integer", "default": 512},
                },
                "required": ["program"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "model_certify",
            "description": (
                "Re-run a problem model and return output hashes plus environment "
                "fingerprint. This certifies provenance only, never official metrics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "program": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "default": 30},
                    "memory_mb": {"type": "integer", "default": 512},
                },
                "required": ["program", "outputs"],
            },
        },
    },
]

# Campaign workers: research tools only (no operator ask_question).
RESEARCH_TOOL_DEFS = list(WIZARD_TOOL_DEFS)

# Mission workers only (never the intake wizard — see MISSION_TOOL_DEFS'
# capability gating below): research tools + B4's workspace-confined
# read/write/edit/grep/find/ls/bash, so `LlmWorkerRuntime` seats get the
# same file-tool parity `PiRuntime` gets natively from the pi CLI.
MISSION_TOOL_DEFS = [*RESEARCH_TOOL_DEFS, *fs_tools.FS_TOOL_DEFS]

# Wizard: research tools + terminal ask_question for UI chips / topic picks.
# Deliberately excludes MISSION_TOOL_DEFS' fs/bash tools — the intake wizard
# is an operator-facing chat with no mission workspace, not a sandboxed
# mission seat.
WIZARD_TOOL_DEFS = [*RESEARCH_TOOL_DEFS, ask_question_tool_schema()]


def _http_get_json(url: str, *, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "sciteam-lab/0.1 (wizard; research)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _http_get_text(url: str, *, timeout: float = 25.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "sciteam-lab/0.1 (wizard; research)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return int(getattr(resp, "status", 200) or 200), raw


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def literature_search(query: str, *, max_results: int = 8) -> dict[str, Any]:
    from harness.adapters.literature import (
        ArxivAdapter,
        CrossrefAdapter,
        EuropePmcAdapter,
        HttpJsonClient,
        MultiSourceLiteratureAdapter,
        OpenAlexAdapter,
    )

    from sciteam.ports.literature import SearchQuery

    client = HttpJsonClient(cache_dir=_CACHE / "literature")
    # arXiv asks for >=3s between API requests; give it its own gate.
    arxiv_client = HttpJsonClient(cache_dir=_CACHE / "literature", min_interval_s=3.0)
    lit = MultiSourceLiteratureAdapter(
        [
            OpenAlexAdapter(client),
            CrossrefAdapter(client),
            EuropePmcAdapter(client),
            ArxivAdapter(arxiv_client),
        ]
    )
    result = lit.search(SearchQuery(text=query, max_results=min(int(max_results), 15)))
    refs = []
    for r in result.references[: max(1, int(max_results))]:
        refs.append(
            {
                "title": r.title,
                "year": r.year,
                "authors": list(r.authors)[:8],
                "doi": r.doi,
                "url": r.url,
                "venue": r.venue,
                "source": r.source,
                "abstract": (r.abstract or "")[:800],
            }
        )
    return {"query": query, "n": len(refs), "references": refs}


def _web_search_ddg(query: str) -> list[dict[str, str]]:
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
    )
    data = _http_get_json(url, timeout=12.0)
    results: list[dict[str, str]] = []
    if data.get("AbstractText"):
        results.append(
            {
                "title": str(data.get("Heading") or query),
                "url": str(data.get("AbstractURL") or ""),
                "snippet": str(data.get("AbstractText") or "")[:500],
            }
        )
    for item in data.get("RelatedTopics") or []:
        if not isinstance(item, dict) or "Text" not in item:
            continue
        results.append(
            {
                "title": str(item.get("Text", ""))[:120],
                "url": str(item.get("FirstURL") or ""),
                "snippet": str(item.get("Text", ""))[:240],
            }
        )
        if len(results) >= 6:
            break
    return results


def _web_search_wikipedia(query: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for lang in ("en", "zh"):
        url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {
                "action": "opensearch",
                "search": query,
                "limit": 5,
                "namespace": 0,
                "format": "json",
            }
        )
        data = _http_get_json(url, timeout=12.0)
        if not isinstance(data, list) or len(data) < 4:
            continue
        titles, descs, links = data[1], data[2], data[3]
        for i, title in enumerate(titles or []):
            results.append(
                {
                    "title": str(title),
                    "url": str((links or [""])[i] if i < len(links or []) else ""),
                    "snippet": str((descs or [""])[i] if i < len(descs or []) else "")[:400],
                }
            )
        if results:
            break
    return results[:6]


def _web_search_bing_html(query: str) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "zh-CN"})
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    results: list[dict[str, str]] = []
    # Bing SERP: <li class="b_algo"> ... <h2><a href="...">title</a></h2> ... <p>snippet
    for block in re.findall(r'(?is)<li class="b_algo".*?</li>', raw)[:8]:
        m = re.search(r'(?is)<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        href = html.unescape(m.group(1))
        title = _strip_html(m.group(2))[:160]
        sm = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = _strip_html(sm.group(1))[:320] if sm else ""
        if title and href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet})
    return results[:6]


def web_search(query: str) -> dict[str, Any]:
    query = (query or "").strip()
    errors: list[str] = []
    backends: list[tuple[str, Any]] = [
        ("duckduckgo", _web_search_ddg),
        ("wikipedia", _web_search_wikipedia),
        ("bing_html", _web_search_bing_html),
    ]
    for name, fn in backends:
        try:
            results = fn(query)
            if results:
                return {"query": query, "source": name, "results": results}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    return {
        "query": query,
        "error": "; ".join(errors) or "no results",
        "results": [
            {
                "title": f"No web results for: {query}",
                "url": "",
                "snippet": "Prefer literature_search, or pass a known URL to url_fetch.",
            }
        ],
        "sources_tried": [b[0] for b in backends],
    }


def url_fetch(url: str, *, max_chars: int = 12000) -> dict[str, Any]:
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "only http/https URLs allowed", "url": url}
    try:
        status, raw = _http_get_text(url)
        text = _strip_html(raw)[: max(500, int(max_chars))]
        return {"ok": True, "status": status, "url": url, "chars": len(text), "text": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "url": url}


def citation_audit(lit_map: dict[str, Any]) -> dict[str, Any]:
    from harness.adapters.literature import (
        ArxivAdapter,
        CrossrefAdapter,
        EuropePmcAdapter,
        HttpJsonClient,
        MultiSourceLiteratureAdapter,
        OpenAlexAdapter,
        audit_references,
    )

    from sciteam.ports.literature import Reference

    client = HttpJsonClient(cache_dir=_CACHE / "literature")
    # arXiv first: for arXiv identifiers the registry itself is authoritative;
    # aggregators have been observed to carry corrupted DOI→work mappings.
    # arXiv asks for >=3s between API requests; give it its own gate.
    arxiv_client = HttpJsonClient(cache_dir=_CACHE / "literature", min_interval_s=3.0)
    lit = MultiSourceLiteratureAdapter(
        [
            ArxivAdapter(arxiv_client),
            OpenAlexAdapter(client),
            CrossrefAdapter(client),
            EuropePmcAdapter(client),
        ]
    )
    # Accept both the tool's own historical shape (`papers`/`references` +
    # `key`) and the `lit_map` exit-contract shape (`entries` + `citation_key`)
    # — models naturally pass their own artifact's field names, and doing so
    # previously silently resolved to "0 references audited" instead of
    # erroring, which is exactly the kind of vacuous-pass a citation audit
    # must not produce (see A1/A2 in HARNESS_SOTA_AUDIT.md).
    papers = lit_map.get("papers") or lit_map.get("references") or lit_map.get("entries") or []
    if not isinstance(papers, list):
        return {"ok": False, "error": "lit_map.papers must be a list"}
    refs: list[Reference] = []
    claims_by_ref: dict[str, list[str]] = {}
    for i, p in enumerate(papers):
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or p.get("citation_key") or p.get("id") or f"ref_{i}")
        refs.append(
            Reference(
                key=key,
                title=str(p.get("title") or ""),
                source=str(p.get("source") or "operator"),
                authors=tuple(str(a) for a in (p.get("authors") or [])[:12]),
                year=int(p["year"]) if str(p.get("year") or "").isdigit() else None,
                doi=str(p["doi"]) if p.get("doi") else None,
                pmid=str(p["pmid"]) if p.get("pmid") else None,
                arxiv_id=str(p["arxiv_id"]) if p.get("arxiv_id") else None,
                url=str(p["url"]) if p.get("url") else None,
                abstract=str(p["abstract"]) if p.get("abstract") else None,
            )
        )
        claims = p.get("claims") or []
        if isinstance(claims, list):
            claims_by_ref[key] = [
                str(c) if not isinstance(c, dict) else str(c.get("text") or c) for c in claims
            ]
    audit = audit_references(refs, port=lit, claims_by_ref=claims_by_ref)
    return audit.to_dict()


def _grounding_papers(payload: Any) -> list[dict[str, Any]]:
    """Flatten grounding entries into an auditable paper list.

    Accepts the shapes a mission's ``artifact_audit`` pointer can produce for
    hypothesis-style contracts: a candidates list, an object with
    ``candidates``, or a bare grounding list. Domain knowledge (what a
    grounding entry is, that an arXiv ID has a DataCite DOI form) lives here in
    the tool — never in the engine that invokes it.

    The norm encoded: a grounding entry that names an identifier must pair it
    with the resolved title. Identifier-less entries are left to the reviewing
    seats' judgment.
    """
    if isinstance(payload, dict):
        payload = payload.get("candidates") or payload.get("grounding") or []
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if "grounding" in item:
            entries.extend(g for g in (item.get("grounding") or []) if isinstance(g, dict))
        elif item.get("arxiv_id") or item.get("doi") or item.get("citation_key"):
            entries.append(item)
    papers: list[dict[str, Any]] = []
    for g in entries:
        arxiv_id = str(g.get("arxiv_id") or "")
        doi = str(g.get("doi") or "") or (f"10.48550/arXiv.{arxiv_id}" if arxiv_id else "")
        if not doi:
            continue
        papers.append(
            {
                "key": str(g.get("citation_key") or f"doi:{doi}"),
                "doi": doi,
                "title": str(g.get("title") or ""),
            }
        )
    return papers


def _dispatch_literature_search(args: dict[str, Any]) -> dict[str, Any]:
    return literature_search(
        str(args.get("query") or ""),
        max_results=int(args.get("max_results") or 8),
    )


def _dispatch_web_search(args: dict[str, Any]) -> dict[str, Any]:
    return web_search(str(args.get("query") or ""))


def _dispatch_url_fetch(args: dict[str, Any]) -> dict[str, Any]:
    return url_fetch(
        str(args.get("url") or ""),
        max_chars=int(args.get("max_chars") or 12000),
    )


def _dispatch_citation_audit(args: dict[str, Any]) -> dict[str, Any]:
    if "payload" in args:
        # Declared-audit path (mission metadata artifact_audit): the engine
        # hands over an artifact slice; the tool maps it to references.
        papers = _grounding_papers(args.get("payload"))
        if not papers:
            # Nothing claimed an identifier: vacuously grounded. Whether
            # identifier-less citations are acceptable is the reviewing
            # seats' call, not this audit's.
            return {"ok": True, "passed": True, "summary": {"total_references": 0}}
        result = citation_audit({"papers": papers})
        if isinstance(result, dict) and "references" in result:
            # For the contract boundary: every named identifier must resolve
            # AND match the title paired with it (claims are not located here;
            # claim support stays with the reviewing seats).
            bad = [
                {
                    "key": r.get("key"),
                    "identifier": r.get("identifier"),
                    "verdict": r.get("verdict"),
                    "title_overlap": r.get("title_overlap"),
                }
                for r in result["references"]
                if r.get("verdict") != "ok"
            ]
            result["ok"] = not bad
            result["passed"] = not bad
            result["failures"] = bad
        return result
    lit_map = args.get("lit_map") or {}
    if not isinstance(lit_map, dict):
        return {"ok": False, "error": "lit_map must be object"}
    return citation_audit(lit_map)


def amendment_audit(proposal: dict[str, Any], lab_root: Path | str | None = None) -> dict[str, Any]:
    """Grundnorm G7 mechanical audit: preflight (schema + fail-closed layer +
    entrenchment) plus motivation-ref resolvability. The tool writes the
    ledger; reviewing seats quote it."""
    root = Path(lab_root) if lab_root else Path(__file__).resolve().parents[1]
    pre = amendment_kernel.preflight(proposal, root / "schemas", assets_root=root / "assets")
    ref_violations = amendment_kernel.audit_alignment(proposal, root) if pre.ok else []
    passed = pre.ok and not ref_violations
    return {
        "ok": passed,
        "passed": passed,
        "layer": pre.layer,
        "preflight_reasons": pre.reasons,
        "flags": pre.flags,
        "ref_violations": ref_violations,
    }


def _dispatch_amendment_audit(args: dict[str, Any]) -> dict[str, Any]:
    proposal = args.get("proposal")
    if not isinstance(proposal, dict):
        return {"ok": False, "error": "proposal must be object"}
    return amendment_audit(proposal)


# Table dispatch — avoids R-B research-stage string branches in the engine (E0).
_WIZARD_TOOL_DISPATCH: dict[str, Any] = {
    "literature_search": _dispatch_literature_search,
    "web_search": _dispatch_web_search,
    "url_fetch": _dispatch_url_fetch,
    "citation_audit": _dispatch_citation_audit,
    "amendment_audit": _dispatch_amendment_audit,
    "ask_question": execute_ask_question,
}


def run_wizard_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    plugins: PluginRegistry | None = None,
) -> dict[str, Any]:
    """Dispatch one wizard tool call.

    Built-in tools (``_WIZARD_TOOL_DISPATCH``) are always checked first and
    can never be shadowed by a plugin (see
    ``docs/25-sciteam-plugin-architecture.md`` §7 — plugins add, they don't
    override). ``plugins`` is an explicit, caller-supplied
    ``PluginRegistry`` (typically produced once by
    ``sciteam.plugin_api.load_all_plugins()`` in the composition root and
    threaded through) — never a module-level global, so a caller that never
    asked for plugins gets byte-identical behavior to before this parameter
    existed.
    """
    args = arguments if isinstance(arguments, dict) else {}
    handler = _WIZARD_TOOL_DISPATCH.get(name)
    if handler is None and plugins is not None:
        handler = plugins.tool_handler(name)
    if handler is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "tool": name}


def _sandbox_handler(
    arguments: dict[str, Any],
    context: dict[str, Any],
    *,
    certify: bool,
) -> dict[str, Any]:
    workspace = context.get("mission_workspace")
    if not workspace:
        return {
            "ok": False,
            "reason_code": "sandbox_workspace_missing",
            "error": "mission workspace was not supplied",
        }
    sandbox = LocalPythonSandbox(str(workspace))
    request = request_from_arguments(arguments)
    result = sandbox.certify(request) if certify else sandbox.run(request)
    payload = result.to_dict()
    if certify:
        payload["scope"] = "model_provenance_only"
        payload["verifier_owned_metrics"] = False
    return payload


_CATALOG = LAB_ROOT / "assets" / "tools" / "catalog.yaml"
_RESEARCH_REGISTRY = ToolRegistry(_CATALOG)
_RESEARCH_REGISTRY.register(
    "literature.search", lambda args, _ctx: _dispatch_literature_search(args)
)
_RESEARCH_REGISTRY.register("literature.audit", lambda args, _ctx: _dispatch_citation_audit(args))
_RESEARCH_REGISTRY.register("web.search", lambda args, _ctx: _dispatch_web_search(args))
_RESEARCH_REGISTRY.register("web.fetch", lambda args, _ctx: _dispatch_url_fetch(args))
_RESEARCH_REGISTRY.register(
    "compute.sandbox",
    lambda args, ctx: _sandbox_handler(args, ctx, certify=False),
)
_RESEARCH_REGISTRY.register(
    "model.certify",
    lambda args, ctx: _sandbox_handler(args, ctx, certify=True),
)
# B4: workspace-confined fs tools + controlled bash (own-arm parity with the
# pi arm's native tool surface — see `sciteam/fs_tools.py`).
_RESEARCH_REGISTRY.register("fs.read", fs_tools.read_file)
_RESEARCH_REGISTRY.register("fs.write", fs_tools.write_file)
_RESEARCH_REGISTRY.register("fs.edit", fs_tools.edit_file)
_RESEARCH_REGISTRY.register("fs.list", fs_tools.list_dir)
_RESEARCH_REGISTRY.register("fs.find", fs_tools.find_files)
_RESEARCH_REGISTRY.register("fs.grep", fs_tools.grep_files)
_RESEARCH_REGISTRY.register("compute.bash", fs_tools.run_bash)


def tool_definitions_for(capabilities: set[str]) -> list[dict[str, Any]]:
    """Capability-filtered tool defs for a mission worker claim.

    ``capabilities`` here is the seat's *effective* set (see
    ``worker_common.effective_tool_capabilities`` — already includes the
    always-on fs.* capabilities and, when justified, `compute.bash`); this
    function does not itself add anything beyond filtering `MISSION_TOOL_DEFS`
    against it, so both arms' "who gets what" logic stays in one place.
    """
    return _RESEARCH_REGISTRY.definitions_for(capabilities, MISSION_TOOL_DEFS)


def tool_permission(name: str) -> str:
    """Catalog permission (`readonly`/`side_effect`/`privileged`) for a tool
    name, used by the worker's tool loop to decide concurrent vs serial
    execution. Unknown names fall back to `side_effect` (serial, safe)."""
    return _RESEARCH_REGISTRY.permission_of(name)


def tool_deterministic(name: str) -> bool:
    """Catalog `deterministic` flag for a tool name (P1-7): gates the
    worker's same-work-item (tool, args) result cache. Unknown names default
    to False (never cached)."""
    return _RESEARCH_REGISTRY.deterministic_of(name)


def run_research_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _RESEARCH_REGISTRY.execute(name, arguments, context=context)
