# Skill: evidence.claim_support

## Purpose
Bind each claim to supporting / contradicting evidence with explicit uncertainty.
Stop “citation decoration” that does not actually back the statement.

## Provenance
Adapted from evidence-first deep-research practices (claim ledgers, counterevidence,
uncertainty-aware synthesis). Aligned with SciTeam citation_audit + KB facts.

## Requires capabilities
- literature.search
- literature.audit
- web.fetch

## Inputs
- Draft claims
- Optional prior lit_map

## Outputs
- `claim_ledger[]`: claim_id, statement, support_spans[], counterevidence[],
  status (`supported`|`contested`|`unsupported`|`unverified`), residual_uncertainty

## Steps
1. For each claim, retrieve or fetch the passages that supposedly support it.
2. Run citation_audit; drop unresolvable ids.
3. Actively search for counterevidence; record it even when inconvenient.
4. Mark residual uncertainty explicitly — do not smooth it away.
5. Training memory is never a support_span.

## Failure modes
- Title-only citations; ignoring contradicting papers; fabricating quotes.

## Stop
Every primary claim has a ledger row with status ≠ empty.
