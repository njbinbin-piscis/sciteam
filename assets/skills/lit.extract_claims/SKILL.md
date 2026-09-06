# SKILL: lit.extract_claims

## Purpose
Reduce a known work to its mission-relevant claims.

## Requires capabilities
- web.fetch
- literature.audit

## Inputs
A precisely identified work (title/year/venue), preferably with a resolvable
id from prior survey/audit.

## Outputs
1–3 claim lines: "X shows Y under condition Z".

## Steps
1. Confirm capabilities; fetch abstract/landing text with tools when needed —
   do not invent what the paper “probably” says from memory.
2. State only what the work demonstrates, not what it discusses.
3. Include the condition/scope — claims without scope are not claims.
4. Mark each claim's relevance to the current mission in one clause.

## Failure modes
Abstract-level paraphrase; importing the work's own hype; merging claims from
different works; fabricating claims from recollection.

## Stop
Claims are atomic, scoped, and attributable.
