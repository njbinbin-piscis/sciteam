# SKILL: org.shadow_verify

## Purpose
Judge a proposal's `prediction.falsifier` against the shadow ledger. The
criterion is frozen outside the proposer's reach; this seat only reads the
ledger and applies the falsifier as written.

## Inputs
`shadow_ledger.json` for the proposal (structural checks section, and the
behavioral comparison section when the layer required one) + the proposal's
`prediction` block.

## Outputs
A verdict: `shadow_pass` or `shadow_fail`, each clause of the falsifier mapped
to the ledger field that decides it.

## Steps
1. No ledger, no verdict. If the shadow run has not been executed, say so and
   stop; do not extrapolate.
2. Structural section: any failed check (asset unloadable, role registry
   broken, paradigm unparsable, guard script failure) is an automatic
   `shadow_fail` regardless of the behavioral numbers.
3. Behavioral section: apply the falsifier condition literally to the
   incumbent-vs-amended comparison. If the falsifier is too vague to be
   applied mechanically, report that as a proposal defect (`shadow_fail`,
   reason: unfalsifiable-as-written).
4. Never re-run or adjust the anchor task set; it is verifier-held and outside
   the amendment surface by whitelist.

## Failure modes
Filling missing ledger fields with expectation; reinterpreting the falsifier
charitably; averaging away a structural failure.

## Stop
Verdict issued with every falsifier clause mapped to a ledger field.
