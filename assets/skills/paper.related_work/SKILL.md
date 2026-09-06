# SKILL: paper.related_work

## Purpose
Position the contribution against the lit_map honestly.

## Requires capabilities
- literature.audit

**Inputs**: lit_map entries (resolvable only); the paper's claims.

**Outputs**: related-work section keyed to citation keys.

**Steps**
1. Group works by the axis they compete on (mechanism, workload, metric).
2. For each group state, in one sentence, what this paper does differently —
   grounded in a frozen result or design component, not adjectives.
3. Cite only lit_map entries; quarantined works may not appear.

**Failure modes**: strawmanning rivals; citing beyond the lit_map.

**Stop**: every cited work has a stated relation to a claim.
