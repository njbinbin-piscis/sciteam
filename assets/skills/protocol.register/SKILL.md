# Skill: protocol.register

## Purpose
Freeze the campaign protocol before any results exist.

## Inputs
- Human or registrar draft goals
- Candidate problem pool (3–5)

## Outputs
- `artifacts/protocol.yaml` (schema-valid)
- KB entry `kind=protocol`

## Steps
1. Enumerate pool with measurable success predicates.
2. Set budgets: rounds, wall clock, eval calls, iterate cycles.
3. Lock eval harness path and baseline path.
4. Validate schema; refuse run start if invalid.

## Failure modes
- Vague goals (“improve algorithms”)
- Success predicate not machine-checkable
- Missing baseline

## Stop
Exit REGISTER only when validator passes.
