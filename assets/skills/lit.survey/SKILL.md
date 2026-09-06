# Skill: lit.survey

## Purpose
Structured literature survey inside the registered topic — not an open essay.

## Requires capabilities
- literature.search
- literature.audit
- web.search
- web.fetch

## Inputs
- `protocol.yaml` topic bounds
- Inclusion / exclusion rubric

## Outputs
- `artifacts/lit_map.json` (papers ≤ N, each with id, year, venue, summary, relevance score, claims[])
- Prefer also writing `citation_audit.json` via the `citation_audit` tool
- KB `kind=citation` entries

## Steps
1. Confirm required capabilities are available; if not, stop with an honest blockage.
2. Generate query set from topic keywords.
3. **Retrieve candidates with the `literature_search` tool** (and `web_search` / `url_fetch` if needed). Do **not** invent DOIs, titles, or abstracts; do **not** use training memory as a paper list.
4. Dedupe by DOI/arXiv; score against rubric; keep top N.
5. For each kept paper, extract 1–3 claims; use `url_fetch` on abstract pages when abstracts are missing.
6. Run `citation_audit` on the draft lit_map; fix unresolvable / mismatched entries before commit.
7. Run `lit.citation_guard` before treating the map as final.

## Failure modes
- Fabricated identifiers (policy violation)
- Using parametric memory as “survey evidence”
- Unresolvable identifiers after audit
- Summaries without claim IDs
- Exceeding N without justification

## Stop
SCOUT cannot exit without valid `lit_map.json` grounded in tool retrieval (or an honest empty/negative survey with documented search queries).
