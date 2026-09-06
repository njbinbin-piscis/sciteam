# Skill: adversary.attack

## Purpose
Attempt to falsify or downgrade the campaign’s claims before writing.

## Inputs
- `claims.json`, `results.json`, `design.md`, `ablation_plan.json`

## Outputs
- `artifacts/attack_report.md`
- Optional `claims.json` patch proposals (downgrades)

## Steps
1. List each claim; ask: what would refute it?
2. Check missing baselines, ablations, statistical weakness, overfitting to instances.
3. Demand one additional eval or explicit claim narrowing.
4. Do not soft-ball; “looks fine” without checks is skill failure.

## Failure modes
- Rubber-stamp approval
- Attacks that ignore protocol constraints

## Stop
ADVERSARIAL phase requires attack_report with ≥1 substantive check per primary claim.
