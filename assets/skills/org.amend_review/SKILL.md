# SKILL: org.amend_review

## Purpose
Review an amendment proposal for legality and evidential grounding. The
review verdict quotes the mechanical audit ledger; the reviewer never
self-certifies compliance.

## Requires capabilities
- institution.audit

## Inputs
One `amendment_proposal` JSON + incumbent asset text + (when present) the
shadow ledger for this proposal.

## Outputs
A review verdict artifact: recommend `shadow` / `reject`, with the
`amendment_audit` tool output embedded verbatim.

## Steps
1. Call `amendment_audit` first. If the tool is unavailable, stop — do not
   review from reading alone.
2. The tool verdict on layer, entrenchment, and reference resolvability is
   authoritative. Your judgment adds: is the diff minimal, is the prediction
   genuinely falsifiable, does the change conflict with other pending
   proposals?
3. A proposal with any audit violation cannot advance regardless of how
   persuasive its narrative is.
4. C-layer proposals additionally require noting that human-gate approval is
   pending — a reviewer cannot green-light a constitutional change alone
   (Grundnorm G7.2).
5. Disagreement with the red team seat must be recorded, not silently
   overridden.

## Failure modes
Reviewing without running the tool; treating a well-written motivation as a
substitute for resolvable refs; approving a C-layer change without flagging
the human gate.

## Stop
Verdict artifact embeds the tool ledger and takes a definite position.
