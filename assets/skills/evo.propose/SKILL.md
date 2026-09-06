# Skill: evo.propose

## Purpose
Turn failure-trace evidence into bounded candidate genome diffs — the Self-Harness
weakness-mining + proposal stages, institutionalized under the evo_proposal contract.

## Inputs
- The active `harness_genome` (mutable surface + frozen paths)
- Failure traces from the base-genome evaluation run (read-only)
- Round budget: K candidates maximum

## Outputs
- `artifacts/evo_proposal_<candidate_id>.json` per candidate (schema `evo_proposal`)
- One unified diff per candidate against the genome overlay (referenced by `diff_ref`)

## Steps
1. Read the base evaluation run; cluster failed tasks into recurring failure patterns.
2. For each pattern (up to K): draft the *minimal* overlay diff that targets exactly that
   mechanism; record resolvable `evidence_ptr` entries into the traces that exhibit it.
3. Check the diff against `constitutional_core.frozen_paths`; fill `scope_check`
   truthfully — the guard recomputes it and a false declaration is a ledgered violation.
4. State `predicted_gain_pp` as an honest estimate; the verifier re-measures it.
5. Emit each proposal artifact under the exit contract; do not evaluate, rank, or promote
   your own candidates.

## Failure modes
- Bundled multi-mechanism diffs → reject at review; one mechanism per candidate.
- Evidence pointers that do not resolve → treated as fabrication, not formatting.
- Prompt-padding edits ("be more careful") without a named failure pattern → not a mechanism.
