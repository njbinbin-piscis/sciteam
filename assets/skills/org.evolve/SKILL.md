# SKILL: org.evolve

## Purpose
**Ungoverned-control-arm only** (`governance: none`, Grundnorm G7.5): apply an
institutional change directly to the org asset copy, skipping preflight,
review, shadow and gates. Exists so the experiment can observe what governed
arms are being compared against — not as a recommended practice.

## Inputs
A change you believe helps, plus write access to `runs/_org/<arm>/assets/`.

## Outputs
The edited asset file(s) and a ledger entry with `status: ungoverned_applied`.

## Steps
1. Confirm the series configuration says `governance: none`. In any governed
   or frozen arm this skill is illegal; using it there is an integrity
   violation, not initiative.
2. Edit the asset under the org asset root directly.
3. You must still append a ledger entry (`ungoverned_applied`) naming the
   target asset and the change — the observation channel stays open even when
   governance is off. Skipping the ledger entry is fabrication by omission.
4. No entrenchment exception: Grundnorm files are still off-limits; the
   engine records and rejects attempts.

## Failure modes
Silent edits with no ledger entry; touching Grundnorm; using this skill in a
governed arm.

## Stop
Asset edited and ledger entry appended.
