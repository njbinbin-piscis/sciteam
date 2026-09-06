---
id: model.as_program
version: "1.0.0"
requires_capabilities:
  - compute.sandbox
  - model.certify
---

# Skill: model.as_program

## Purpose
Externalize an uncertain problem model as executable, falsifiable code before
using it to plan, rank, or finalize an artifact.

## Inputs
- Append-only evidence timeline with stable event identifiers
- Current assumptions and target predicate
- Mission workspace path

## Outputs
- `model.py`: state, transition/mechanism, observation and target predicates
- `model_cases.json`: historical cases keyed to evidence event identifiers
- `model_report.json`: predictions, mismatches, revisions and output hashes

## Steps
1. Write the smallest model that makes the current assumptions explicit. Keep
   domain-specific mechanisms in this model, never in the engine.
2. Add historical cases in chronological order. Do not train on or rewrite
   future observations.
3. Call `compute_sandbox` with declared inputs and outputs. A mismatch is a
   counterexample: preserve it in `model_report.json`.
4. Revise assumptions or transitions, not the evidence. Re-run all prior cases.
5. Call `model_certify`; cite the environment fingerprint and output hashes.
6. Use the model for planning only after every claimed invariant has a passing
   case or is explicitly marked unresolved.

## Integrity rules
- Sandbox results support reasoning and diagnosis only.
- Official benchmark or exit-contract metrics remain verifier-owned.
- No network, host shell, absolute paths, parent traversal, undeclared inputs,
  or privileged tools.
- Never delete a mismatch; supersede an old model with a new version.

## Failure modes
- Model fits only the latest event or reads future evidence
- Hidden state or target predicate exists only in prose
- A hash/provenance run is presented as an official evaluation
- Repeated mismatch without changing the model

## Stop
Stop when the certified report records all historical cases, mismatches and
remaining uncertainty, or escalate with the failing case and attempted models.
