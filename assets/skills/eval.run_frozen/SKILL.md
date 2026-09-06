# Skill: eval.run_frozen

## Purpose
Produce the only official metrics for the campaign via the injected verifier
(`run_eval` in the worker envelope). Never hand-write `results.json`.

## Requires capabilities
- compute.eval

## Inputs
- Locked eval entrypoint from protocol / mission inputs
- For **al_screen**: a candidate acquisition module you wrote under the mission
  directory exposing `acquire(model, pool_X, rng, batch, state) -> indices` and
  optional `NAME`; set `run_eval.problem_id = "al_screen"` and
  `run_eval.candidate_file` to that filename
- For algo problems: candidate `.py` implementing the frozen interface
- Seeds from mission inputs / prior protocol

## Outputs
- Verifier-owned `results.json` at the mission artifact path (baseline vs
  candidate metrics, `eval_script_sha256`, `candidate_sha256`, `success`)

## Steps
1. Write (or revise) the candidate source file into the mission directory.
2. Emit `run_eval` — do **not** invent metrics or emit a hand-written artifact
   when `artifact_source: verifier`.
3. Read the returned results; if the completion gate fails, analyse and iterate.
4. Commit KB `kind=result` linked to claim_ids under test.

## Failure modes
- Hand-written results.json (integrity violation)
- Missing `acquire` / wrong problem_id
- Partial runs reported as full success
- Missing seeds

## Stop
EVALUATE success iff schema-valid verifier results and correctness gate pass.
