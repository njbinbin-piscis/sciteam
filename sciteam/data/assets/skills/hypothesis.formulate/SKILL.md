# SKILL: hypothesis.formulate

## Purpose
Turn idea cards into falsifiable candidate hypotheses.

## Requires capabilities
- literature.search
- literature.audit

**Inputs**: idea cards / survey claims from team context; the registered
problem's frozen success predicate.

**Outputs**: hypothesis entries: `statement`, `falsification_criterion`,
`rationale`, `grounding[]`, optional `smoke_check`.

**Steps**
1. Restate the idea as a mechanism claim: *doing X changes Y because Z*.
2. Bind it to the frozen eval: which measured metric moves, by how much, in
   which direction? If it cannot move a frozen metric, discard it.
3. Write the falsification criterion as the concrete eval outcome that would
   kill the hypothesis.
4. Attach grounding citations: find each with `literature_search`, verify with
   `citation_audit`, and record the resolved identifier + exact title. A
   citation you did not see in a tool result may not be attached — missions
   commonly declare a citation audit over the exit artifact, and unresolvable
   or misattributed identifiers will fail the contract for the whole team.
   If nothing resolvable supports the idea, mark it ungrounded.
5. Propose the cheapest smoke check runnable inside the mission budget.

**Failure modes**: unfalsifiable phrasing ("improves quality"); mechanism
that the eval interface cannot express; grounding by vibe.

**Stop**: 1–3 candidates, each falsifiable and interface-compatible.
