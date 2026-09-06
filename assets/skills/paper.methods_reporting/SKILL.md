# Skill: paper.methods_reporting

## Purpose
Write methods / experimental protocol text that a peer could audit: what was
run, what was frozen, what was not tried, and where numbers come from.

## Provenance
Distilled from Orchestra Research `ml-paper-writing` reporting discipline.
Numbers must quote verifier-owned artifacts; no hand-written result tables.

## Requires capabilities
- compute.eval

## Inputs
- Protocol / design artifacts
- Verifier `results.json` paths
- Ablation / seed notes

## Outputs
- Methods section draft (or `methods.md`) with: interface, data splits, seeds,
  baselines, compute budget, and explicit “not evaluated” list

## Steps
1. Quote eval entrypoint / script hash / seed list from protocol or results.
2. Describe only procedures that were actually executed or are scheduled.
3. Every metric sentence points to an artifact field; otherwise mark `unverified:`.
4. State negative / null results without euphemism.

## Failure modes
- Marketing language; metrics from memory; hiding failed ablations.

## Stop
Methods text is reproducible on paper and ledger-aligned.
