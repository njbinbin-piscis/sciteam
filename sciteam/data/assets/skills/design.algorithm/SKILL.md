# SKILL: design.algorithm

**Purpose**: derive an implementable design from a hypothesis, bound to the
frozen eval interface.

**Inputs**: winning hypothesis; frozen eval interface docs; mission budget.

**Outputs**: design artifact fields: `summary`, `components[]`,
`interface_compliance`, `parameters[]`, `risks[]`.

**Steps**
1. Copy the exact interface signatures from the eval script docstring into
   the design; every component must fit inside them.
2. Decompose the mechanism into the smallest set of named components; for
   each: what changes, expected effect, complexity note.
3. Define tunable parameters with defaults; mark which are load-bearing.
4. State the riskiest assumption and how the first eval run will expose it.

**Failure modes**: designing around the simulator instead of within it;
components with no observable effect on the frozen metrics.

**Stop**: an implementer could code this without asking questions.
