# Skill: paper.write_imrad

## Purpose
Assemble a venue-shaped manuscript from frozen artifacts only.

## Inputs
- `outline.md`, `hypothesis.md`, `design.md`, `lit_map.json`, `results.json`, `attack_report.md`

## Outputs
- `artifacts/paper/main.tex` (or md) + sections
- Cross-ref map claim_id → result_id → table/figure

## Steps
1. Load outline; allocate sections to claim_ids.
2. Related Work: only papers in `lit_map.json`.
3. Experiments: every number must cite `results.json` key.
4. Discussion: include adversary issues and limitations.
5. Run `paper.checklist`; fail skill if critical gaps.

## Failure modes
- Free-typed metrics
- Citations not in lit_map
- Ignoring attack_report

## Stop
WRITE exits only with checklist critical=pass.
