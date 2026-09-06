# SKILL: package.arm

**Purpose**: assemble the evidence bundle (trace / execution / skill /
knowledge-graph components) for archival or submission.

**Inputs**: run directory: protocol, traces, KB, exports, results, manuscript.

**Outputs**: package_manifest artifact listing every component with paths
(and hashes where available); honest `outcome` + `missing[]`.

**Steps**
1. Enumerate required kinds: protocol, playbook/planner log, mission traces,
   paradigms_used, engine_diff_stat, three_questions, official results,
   manuscript, checklist.
2. Verify each file exists and hash it; absent items go to `missing[]`.
3. Label the outcome honestly: positive / negative / partial.
4. Confirm the bundle alone lets a stranger replay the frozen eval.

**Failure modes**: manifest pointing at files that were later edited;
claiming `positive` for an unmet predicate.

**Stop**: manifest validates and covers all produced evidence.
