---
name: institution.round_assess
description: >
  Produce a round_assessment envelope from an opaque observation pack and
  charter text. Used by may_assess_round seats only.
---

# institution.round_assess

## When to use

After a production wave is quiescent, when your seat holds `may_assess_round`.

## Procedure

1. Read `assessment_facts` (no decisions inside — observations only).
2. Read charter / protocol text in the same pack.
3. Decide `continue` / `completed` / `failed` with a `reason_code`.
4. Return **one JSON object** matching `schemas/round_assessment.schema.json`.
5. Do not write the mission exit-contract file.

## Hard output contract (non-negotiable)

Reply with **JSON only** — no markdown fences, no prose outside the object.

Required keys:

| key | values |
|---|---|
| `decision` | `continue` \| `completed` \| `failed` |
| `reason_code` | `ok_complete` \| `need_artifact` \| `gate_unsatisfied` \| `exit_stalled` \| `integrity_fail` \| `process_fail` \| `all_workers_failed` |
| `summary` | short rationale (string) |
| `next_round_hint` | guidance for production seats, or `""` |
| `assessor_key` | your seat key |

Optional: `approve_promote_draft` (bool).

**Anti-patterns (cause mechanical CONTINUE + repair hint, not scientific stop):**

- Returning only a natural-language summary
- Wrapping JSON in ``` fences without a parseable object
- Using a non-whitelist `reason_code`
- Emitting `envelope.artifact` for the mission exit file

## Disposition guidance

- Missing exit artifact → `continue` / `need_artifact` (hint emit-duty seats).
- Artifact present + charter satisfied → `completed` / `ok_complete`.
- Stagnation threshold reached with no artifact → `failed` / `exit_stalled` (fail-honest).
- Do **not** invent science content to force `completed`.

## Non-goals

- Not a production specialist seat.
- Not allowed to emit `emits_exit_artifact` payloads.
