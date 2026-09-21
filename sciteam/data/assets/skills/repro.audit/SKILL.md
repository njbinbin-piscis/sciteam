# Skill: repro.audit

## Purpose
Audit whether the campaign package is independently reproducible enough to
ship: seeds, hashes, interfaces, data provenance, and missing pieces.

## Provenance
Common scientific reproducibility checklists (community consensus), adapted to
SciTeam package.arm / protocol.register fields.

## Requires capabilities
- compute.eval

## Inputs
- Protocol, results.json, candidate sources, package manifest draft

## Outputs
- `repro_report`: checklist pass/fail rows, blockers[], severity

## Steps
1. Verify eval script identity (path + hash) matches protocol lock.
2. Verify seeds / splits / candidate hashes are recorded.
3. Flag any metric not present in verifier output.
4. If `dataset.materialize` was required but unavailable, record honest blockage
   (do not invent dataset hashes).
5. Prefer fail-closed: missing row ⇒ not shippable.

## Failure modes
- Checklist theatre; accepting “available on request” without artifact pointers.

## Stop
repro_report with zero unresolved P0 blockers, or an honest non-ship decision.
