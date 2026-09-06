# SKILL: org.request_retro

## Purpose
Request that the campaign harness run a retrospective (`m_retro`) after this
run, instead of waiting for the operator to decide. This is the line F
model-callable entry point (ENGINE_ISA.md §2.11, `O_RETRO_INVOKE`) — whether
a retrospective happens has, until now, always been the operator's CLI
choice; this skill makes it something a seat can ask for.

## Inputs
Whatever you observed during this mission (a repeated failure pattern, a
paradigm that stalled, a rule you believe is miscalibrated). No special tool
is required: this skill only tells you the filename and shape to use with
your existing file-write capability.

## Outputs
A single JSON file named `retro_request.json`, written to the **root of your
own mission workspace** (not a subdirectory), matching
`schemas/retro_request.schema.json`:

```json
{
  "requested_by": "<your agent_key>",
  "reason": "<one or two sentences, specific, tied to something you saw>",
  "requested_at": "<ISO-8601 timestamp>",
  "target_asset_hint": "<optional: which asset you think should be reviewed>"
}
```

## Steps
1. Decide honestly whether a retrospective is warranted — this is not a
   required step of any mission; writing nothing is a legal and unremarkable
   outcome.
2. If warranted, write `retro_request.json` with the shape above.
3. Continue your mission normally. Writing this file does **not** grant you
   any authority over the organization's assets: the harness may or may not
   act on it, and if it does, the resulting retrospective still drafts
   proposals that go through the full existing amendment pipeline (preflight
   → shadow → cross-seat review → promotion). You are not proposing an
   amendment here — you are only asking that someone look.
4. Do not write this file speculatively "just in case" or to game any
   measured trait — `reason` must reference something concrete you actually
   observed in this mission (audited the same way every other exit claim is:
   G2 mechanical-record discipline applies to this file too, even though it
   is not itself an exit contract).

## Failure modes
Writing the file with a vague or fabricated `reason`; writing it outside the
mission workspace root (the harness only scans the documented location);
treating this as a substitute for `org.retro`'s own drafting duty (this
skill only *requests* a retrospective, it does not perform one).

## Stop
File written (or honestly not written) once per mission at most.
