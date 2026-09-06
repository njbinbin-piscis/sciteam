# SKILL: org.amend_draft

## Purpose
Draft a single `amendment_proposal` from one confirmed failure pattern:
a persistent, falsifiable, revertible change to C/L/P institutional assets.

## Inputs
One `retro_report` failure pattern (with its evidence_refs) plus the incumbent
asset text at `target_asset`.

## Outputs
One JSON object matching `schemas/amendment_proposal.schema.json`, status
`drafted`, written under the mission workspace and registered in the ledger.

## Steps
0. Use the exact schema field names — improvised synonyms are quarantined
   unread. Top-level required: `amendment_id`, `target_asset`, `change_kind`
   (`add`|`remove`|`modify`), `diff`, `motivation` (`failure_refs`, `pattern`),
   `prediction` (`metric`, `direction` one of `+`|`-`|`0`, `scope`,
   `falsifier`), `rollback`, `proposer`, `status: drafted`. The mission input
   `proposal_contract` carries the schema verbatim plus a minimal example.
1. One proposal = one target asset = one coherent change. No omnibus bills.
2. `target_asset` is a path relative to the org asset root. The layer is
   derived mechanically from the path whitelist — do not argue the layer,
   `declared_layer` is informational only. Grundnorm assets are entrenched;
   proposing against them will be rejected and logged.
3. `diff` for `modify` is a strict unified diff against the incumbent text
   you actually read; context mismatch means mechanical rejection. For `add`
   it is the full file content.
4. `motivation.failure_refs` must copy the resolvable refs from the retro
   report — the audit tool will resolve each one.
5. `prediction` must name a metric, direction, scope, and a concrete
   `falsifier` that the shadow ledger can trigger. "Improves quality" with no
   falsifier is not a prediction.
6. State the `rollback` path explicitly.

## Failure modes
Vague predictions; diffs written from memory instead of the incumbent text;
bundling several changes in one proposal; targeting entrenched assets.

## Stop
Proposal validates against the schema and preflight passes with your derived
layer.
