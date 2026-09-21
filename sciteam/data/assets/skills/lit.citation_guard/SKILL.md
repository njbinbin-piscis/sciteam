# SKILL: lit.citation_guard

## Purpose
Keep fabricated or unverifiable citations out of artifacts.

## Requires capabilities
- literature.audit
- literature.search
- web.fetch

## Inputs
Any citation about to enter an artifact.

## Outputs
`resolvable: true` entries, or quarantine records.

## Steps
1. Confirm `literature.audit` is available; if not, stop — do not “eyeball” citations from memory.
2. A citation is resolvable only when title + year + venue/arXiv id are all
   stated and internally consistent, preferably after `citation_audit`.
3. Anything uncertain goes to `quarantined` with the reason; a quarantined
   work may be mentioned as "unverified recollection", never cited as fact.
4. Audit the artifact's resolvable rate against the campaign threshold.

## Failure modes
Plausible-sounding synthesis of two real papers into one fake one; year/venue
drift; treating training memory as resolution.

## Stop
Zero unquarantined uncertain citations in the artifact.
