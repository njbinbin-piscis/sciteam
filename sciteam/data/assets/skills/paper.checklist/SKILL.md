# SKILL: paper.checklist

**Purpose**: close the manuscript checklist truthfully.

**Inputs**: manuscript draft; claim_result_map; citation audit.

**Outputs**: checklist artifact with per-item status + evidence.

**Steps**
1. For each quantitative claim: locate its result id, open the frozen
   results file, confirm the number matches exactly.
2. Run the citation audit: resolvable rate vs campaign threshold.
3. Confirm the adversarial report exists and its verdict is reflected in the
   manuscript's claims.
4. Mark critical items `fail` when they fail — a failing checklist that is
   honest beats a passing one that lies.

**Failure modes**: rubber-stamping; "n/a" as an escape hatch for critical
items.

**Stop**: every item has status + evidence; failures are actionable.
