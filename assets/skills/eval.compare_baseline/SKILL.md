# SKILL: eval.compare_baseline

## Purpose
Interpret frozen-eval output against the registered baseline fairly.

## Requires capabilities
- compute.eval

**Inputs**: results.json (frozen eval output only).

**Outputs**: comparison summary: metric deltas, predicate verdict, the most
informative gap.

**Steps**
1. Read baseline and candidate metrics from the SAME results.json.
2. Compute deltas exactly as the registered predicate defines them.
3. State the verdict verbatim from the `success` field; never re-adjudicate.
4. If failed: identify which predicate arm failed and by how much — that
   number drives the next iteration decision.

**Failure modes**: comparing across different eval runs/seeds; citing numbers
not present in results.json.

**Stop**: verdict + gap analysis delivered.
