# SKILL: domain.algo.complexity_check

**Purpose**: sanity-check a design's asymptotic and constant-factor costs
before implementation.

**Inputs**: design components; workload scale from the frozen protocol.

**Outputs**: per-component cost note; a verdict: affordable / risky / fatal.

**Steps**
1. For each component, state the cost per operation the eval measures
   (per access / per admission / per query).
2. Multiply by the frozen workload scale; compare against the baseline's
   costs — a policy that wins hit rate but 10x per-access cost may be fine
   (simulator counts hits) ONLY if the protocol says so; read the predicate.
3. Flag any component whose cost grows with total history (unbounded state).

**Failure modes**: big-O reasoning that ignores the metric actually measured.

**Stop**: verdict recorded with the dominating term identified.
