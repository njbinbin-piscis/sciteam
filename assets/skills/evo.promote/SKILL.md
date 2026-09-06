# Skill: evo.promote

## Purpose
Convert audited proposal + verification artifact pairs into append-only, hash-chained
evolution-ledger decisions; merge accepted diffs into the next genome generation.

## Inputs
- Proposal and verification artifacts for each candidate (read-only, already audited)
- Current `evolution_ledger.jsonl` tail (for `prev_entry_hash`)
- Batch `engine_commit`

## Outputs
- One `evolution_ledger_entry` (schema) appended per candidate, promoted or not
- Next-generation genome file (`parent` set) merging all promoted diffs

## Steps
1. For each candidate, check the audit verdict and verifier verdict; `promote: true`
   requires `confirmed` + audit `pass` + three distinct seats across the artifacts.
2. Choose exactly one primary `reason_code` for every rejection.
3. Compute `prev_entry_hash` from the previous ledger line; append — never rewrite,
   reorder, or delete. A wrong entry is corrected by a new entry referencing it.
4. After the round: merge promoted diffs into a new genome with `parent` lineage and
   the batch `engine_commit`; frozen paths are inherited unchanged.

## Failure modes
- Editing a proposal/verification artifact "to fix it" → hard prohibition; reject instead.
- Ledger entry without resolvable `proposal_ref`/`verification_ref` → audit fails.
- Promoting with seats not distinct → `seats_distinct` audit check fails the entry.
