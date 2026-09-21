# SKILL: paper.method

**Purpose**: describe the design precisely enough for re-implementation.

**Inputs**: design artifact; final candidate module.

**Outputs**: method section.

**Steps**
1. Describe components in the design artifact's decomposition and naming.
2. State the eval interface contract explicitly (what is given, what returns).
3. Parameters get their actual final values; no untested defaults in prose.
4. Include the mechanism argument (why it should work), scoped to what the
   adversarial report left standing.

**Failure modes**: prose drifting from the code that produced the results.

**Stop**: a reader could rebuild the candidate from this section alone.
