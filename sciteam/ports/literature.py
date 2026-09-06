"""Literature port: resolvable references and located claim spans.

Rule enforced by adapters (not by this module): a raw search snippet is NEVER
evidence. Only a resolved :class:`Reference` plus a :class:`ClaimSpan` that
locates the claim inside the source may enter the knowledge base.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


def content_hash(payload: Any) -> str:
    """Stable content hash of any JSON-serialisable payload."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Reference:
    """A resolvable bibliographic record from a single source database."""

    key: str
    title: str
    source: str  # which database resolved it: openalex / crossref / pubmed / ...
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None
    pmid: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    venue: str | None = None
    abstract: str | None = None
    license: str | None = None
    retrieved_at: str = ""
    raw_hash: str = ""

    def identity(self) -> str:
        """Best available stable identifier for dedup."""
        return self.doi or self.pmid or self.arxiv_id or self.title.strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "source": self.source,
            "authors": list(self.authors),
            "year": self.year,
            "doi": self.doi,
            "pmid": self.pmid,
            "arxiv_id": self.arxiv_id,
            "url": self.url,
            "venue": self.venue,
            "abstract": self.abstract,
            "license": self.license,
            "retrieved_at": self.retrieved_at,
            "raw_hash": self.raw_hash,
        }


@dataclass(frozen=True)
class SearchQuery:
    text: str
    sources: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    max_results: int = 25

    def cache_key(self) -> str:
        return content_hash(
            {
                "text": self.text,
                "sources": sorted(self.sources),
                "filters": self.filters,
                "max_results": self.max_results,
            }
        )


@dataclass(frozen=True)
class SearchResult:
    query: SearchQuery
    references: tuple[Reference, ...]
    retrieved_at: str
    raw_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": {
                "text": self.query.text,
                "sources": list(self.query.sources),
                "filters": self.query.filters,
                "max_results": self.query.max_results,
            },
            "retrieved_at": self.retrieved_at,
            "raw_hash": self.raw_hash,
            "references": [r.to_dict() for r in self.references],
        }


@dataclass(frozen=True)
class ClaimSpan:
    """A claim located inside a specific reference, with a support verdict."""

    reference_key: str
    claim: str
    span_text: str
    locator: str  # e.g. "abstract" / "sec:results:p3" / "fig:2"
    support: str = "unverified"  # supports / refutes / partial / unverified
    verified_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_key": self.reference_key,
            "claim": self.claim,
            "span_text": self.span_text,
            "locator": self.locator,
            "support": self.support,
            "verified_by": self.verified_by,
        }


@runtime_checkable
class LiteraturePort(Protocol):
    """Deterministic, cache-backed literature access."""

    def search(self, query: SearchQuery) -> SearchResult: ...

    def fetch(self, identifier: str) -> Reference | None: ...

    def locate(self, reference: Reference, claim: str) -> ClaimSpan | None: ...
