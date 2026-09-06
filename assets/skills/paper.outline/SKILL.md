# SKILL: paper.outline

**Purpose**: fix the manuscript's claim structure before prose exists.

**Inputs**: KB claims, frozen results, attack report verdict.

**Outputs**: section outline where every section lists the claims it carries
and each claim's evidence pointer (result id / citation key).

**Steps**
1. List the paper's claims first; order sections around them (IMRaD).
2. Attach evidence pointers now — a claim with no pointer is cut here, not
   discovered missing at checklist time.
3. Mark the honesty items: limitations, negative findings, scope.

**Failure modes**: outlining by section names instead of claims.

**Stop**: every planned claim has an evidence pointer.
