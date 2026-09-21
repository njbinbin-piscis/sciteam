# SKILL: institution.role_pipeline

## Purpose
Design a **wave-internal Finish-to-Start stage graph** for tournament / committee
work so seats do not claim in parallel when science requires ordered handoff.

This is an **institutional** skill (L2 assets), not an engine phase enum.

## Inputs
- Campaign topic / mission goal (scientific content)
- Paradigm draft or existing team YAML
- Exit contract name (e.g. `candidate_hypotheses`)

## Outputs
- `coordination.work_graph.role_pipeline: [role…]` on the paradigm
- Exactly one member with `emits_exit_artifact: true`
- Mission `metadata.stagnation_max_rounds` (>0)
- Duty-first stage briefs (YOUR JOB / DELIVERABLE / NOT YOUR JOB) — not a
  meta-narrative Goal line that buries the seat

## Steps
1. List the scientific stages that must be ordered (e.g. generation →
   reflection → ranking → evolution → proximity → meta_review).
2. Put that list in `role_pipeline`. Engine compiles FS edges per wave.
3. Keep `scheduling.mode: barrier` for **inter-wave** join; do **not** set
   `max_parallel` equal to seat count unless stages are intentionally parallel.
4. Grant exit-artifact emit to **one** seat only; others deliver via
   summary/facts + predecessor digest.
5. Set `stagnation_max_rounds` so empty CONTINUE loops fail-honest.
6. Acceptance: fake or live harness shows claim order = pipeline; exit file
   appears only after emit seat; observatory gantt shows FS slices not
   full-span parallel bars.

## Failure modes
- Six seats + `max_parallel: 6` with empty pipeline → fake tournament
- Every seat allowed to write exit JSON → emit deadlock or garbage overwrite
- Mission Goal written as ISA manifesto so workers ignore DELIVERABLE
- Relying on engine `Phase.*` enums instead of `role_pipeline`

## Stop
Paradigm YAML + mission metadata satisfy the acceptance table in
`assets/committees/TOURNAMENT_INSTITUTION_BRIEF.md`.

## Verified reference (2026-07-31 live; supersedes the 2026-07-19 run this
section previously cited, which has since been pruned)
- Run: `runs/isa_hyp_tournament_live_20260731T011057Z`
- Order: generation→reflection→ranking→evolution→proximity→meta_review (1 wave)
- `contract_ok: true`, artifact `missions/m_hyp_isa/candidate_hypotheses.json`
- Harness: `harness/run_isa_hyp_tournament.py --mode live|fake`
