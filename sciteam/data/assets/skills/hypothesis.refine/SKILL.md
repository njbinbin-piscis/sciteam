# SKILL: hypothesis.refine

**Purpose**: sharpen or narrow a surviving hypothesis after critique or a
failed eval round.

**Inputs**: the hypothesis, the objection or failure evidence.

**Outputs**: a revised hypothesis entry (new id, provenance link to the old).

**Steps**
1. Identify exactly which part the evidence contradicts: mechanism, scope,
   or magnitude.
2. Prefer narrowing scope over weakening the claim ("holds when prefix share
   > 30%" beats "sometimes helps").
3. Re-derive the falsification criterion for the revised form.
4. Record the old id in `rejected_ideas`/KB with the reason.

**Failure modes**: post-hoc rationalization that immunizes the hypothesis
against any eval outcome.

**Stop**: revised hypothesis is again falsifiable, or the line is abandoned
explicitly.
