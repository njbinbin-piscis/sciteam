# Skill: review.epistemic_rigor

## Purpose
Epistemic peer-review of a mission or campaign package: score whether evidence
actually supports claims, and whether the research process is honestly documented.

## Provenance
Distilled from Orchestra Research `ara-rigor-reviewer` six dimensions (MIT).
Bound to SciTeam artifacts (`claims`, verifier `results.json`, lit_map, traces)
instead of ARA directory layout.

## Requires capabilities
- literature.audit

## Inputs
- Candidate claims / paper draft sections
- Verifier-owned results (if quantitative)
- lit_map / citation quarantine
- Exploration / failure notes from team facts

## Outputs
- `rigor_report`: scores 1–5 on D1–D6, strengths/weaknesses, severity-ranked
  issues, overall recommendation (`strong_accept`|`accept`|`revise`|`reject`)

### Dimensions
| Id | Focus |
|---|---|
| D1 | Evidence relevance — cited evidence supports the claim in substance |
| D2 | Falsifiability quality — criteria actionable and scoped |
| D3 | Scope calibration — claims assert no more than evidence warrants |
| D4 | Argument coherence — problem → method → evidence arc |
| D5 | Exploration integrity — failures and dead ends recorded |
| D6 | Methodological rigor — baselines, ablations, reporting |

## Steps
1. Inventory claims, experiments/evals, and grounding refs from artifacts — do not invent missing pieces.
2. Score each dimension with concrete pointers into artifacts.
3. Downgrade any claim whose numbers disagree with verifier ledger.
4. Prefer constructive, severity-ranked feedback; rubber-stamp is skill failure.

## Failure modes
- Reviewing style instead of epistemology; ignoring negative results; inventing citations.

## Stop
One rigor_report with scores + actionable issues; reject path is legitimate.
