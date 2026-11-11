"""Literature search for realworld runs: Crossref and arXiv, host side (the sandbox needs no
network for it). Every hit is returned with its source URL and also appended to
/workspace/refs/citations.jsonl so a claim can be traced back to a real record."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from sciteam.harness.tools.base import ToolContext, ToolResult
from sciteam.providers.llm.types import ToolSpec

CITATIONS = "refs/citations.jsonl"
Fetch = Callable[[str], bytes]


def _http(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sciteam/1.0 (literature_search)"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https endpoints
        return r.read()


def crossref(query: str, n: int, fetch: Fetch) -> list[dict[str, Any]]:
    url = ("https://api.crossref.org/works?rows=%d&select=DOI,title,author,issued,URL"
           "&query=%s" % (n, urllib.parse.quote(query)))
    items = json.loads(fetch(url)).get("message", {}).get("items", [])
    out = []
    for it in items:
        year = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        out.append({"source": "crossref", "title": (it.get("title") or [""])[0],
                    "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip()
                                for a in it.get("author", [])][:6],
                    "year": year, "doi": it.get("DOI"), "url": it.get("URL")})
    return out


def arxiv(query: str, n: int, fetch: Fetch) -> list[dict[str, Any]]:
    url = ("https://export.arxiv.org/api/query?max_results=%d&search_query=all:%s"
           % (n, urllib.parse.quote(query)))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(fetch(url)).findall("a:entry", ns):
        g = lambda tag: (e.findtext(f"a:{tag}", default="", namespaces=ns) or "").strip()  # noqa: E731
        out.append({"source": "arxiv", "title": " ".join(g("title").split()),
                    "authors": [a.findtext("a:name", default="", namespaces=ns)
                                for a in e.findall("a:author", ns)][:6],
                    "year": int(g("published")[:4] or 0) or None, "doi": None, "url": g("id")})
    return out


class LiteratureSearchTool:
    readonly = False
    spec = ToolSpec(
        name="literature_search",
        description=("Search the scholarly literature (Crossref and arXiv). Returns title, "
                     "authors, year, DOI and URL for each hit and records them in "
                     "refs/citations.jsonl. Cite only what appears there."),
        parameters={"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "per source, default 5, max 10"}},
            "required": ["query"], "additionalProperties": False})

    def __init__(self, fetch: Fetch = _http):
        self.fetch = fetch

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        q, n = str(args["query"]).strip(), max(1, min(10, int(args.get("max_results") or 5)))
        if not q:
            return ToolResult(content="error: empty query", is_error=True)
        hits, errors = [], []
        for fn in (crossref, arxiv):
            try:
                hits += await asyncio.to_thread(fn, q, n, self.fetch)
            except Exception as exc:  # noqa: BLE001 - report which source failed
                errors.append(f"{fn.__name__}: {exc}")
        if hits:
            try:
                old = (await ctx.sandbox.read_file(CITATIONS)).decode()
            except Exception:  # noqa: BLE001 - first citation
                old = ""
            lines = [json.dumps({**h, "query": q}, ensure_ascii=False) for h in hits]
            await ctx.sandbox.write_file(CITATIONS, (old + "\n".join(lines) + "\n").encode())
        body = json.dumps(hits, ensure_ascii=False, indent=1)
        if errors:
            body += "\nunavailable: " + "; ".join(errors)
        return ToolResult(content=body, is_error=not hits and bool(errors),
                          meta={"hits": len(hits)})


REALWORLD_TOOLS = {"literature_search": LiteratureSearchTool()}
