# SKILL: org.retro

## Purpose
Turn a finished campaign's mechanical record into a `retro_report`: failure
patterns with resolvable evidence, lessons, and zero or more amendment
proposal ids. This is the organization's double-loop entry point (Grundnorm G7).

## Inputs
Campaign record in the pack: ledgers, audit reports, round assessments,
scorecards, usage summary. Narrative recollection is **not** an input.

## Outputs
One JSON object matching `schemas/retro_report.schema.json`, written as the
mission exit artifact.

## Steps
1. Read the mechanical record only; every `failure_patterns[].evidence_refs`
   entry must be a resolvable `runs/...` artifact path you actually saw.
2. Classify each pattern's `layer_hypothesis`: P (skill/prompt), L (paradigm/
   template/catalog), C (constitution), engine, environment.
3. `engine` and `environment` patterns are **escalations to the human
   operator** — they must NOT become amendment proposals (E0 discipline).
4. Improvements that need no rule change go to `single_loop_notes`, not to
   proposals. Keep the two loops separate.
5. An honest "nothing to amend" (`proposal_ids: []`) is a legal and welcome
   terminal state. Do not invent failure patterns to look thorough.

## Failure modes
Fabricated evidence_refs; smuggling engine complaints into P/L/C proposals;
padding the report with patterns no artifact supports.

## Stop
Report validates against the schema and every ref resolves.
