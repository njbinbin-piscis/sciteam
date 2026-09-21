# Skill: evo.verify

## Purpose
Mechanically re-measure one candidate genome diff on held-in and held-out task packs,
producing the numbers the promotion decision and the fabrication-rate metric rest on.

## Requires capabilities
- compute.eval

## Inputs
- One candidate proposal artifact + its diff (read-only)
- Base genome and scratch overlay workspace
- Held-in task manifest; held-out task manifest (verifier-held, never disclosed)

## Outputs
- `artifacts/evo_verification_<candidate_id>.json` (schema `evo_verification`)
- Raw evaluation outputs under `verify_run_ref` backing every reported number

## Steps
1. Apply the diff to a scratch overlay; never modify the diff or the baseline assets.
2. Run the held-in pack under base and candidate genomes; record per-task outcomes.
3. Run the held-out pack the same way; only aggregate counts leave this mission.
4. Compute `gain_pp` per split; list every base-pass→candidate-fail task in `regressions`.
5. Assign the verdict strictly per PREREG thresholds; emit the artifact under contract.

## Failure modes
- Reporting numbers not recomputable from `verify_run_ref` → artifact_audit fails the mission.
- Softening regressions because aggregate gain is positive → integrity violation.
- Leaking held-out task identities into any artifact readable by the proposer.
