# SKILL: plan.revise

**Purpose**: propose *additions* to the running mission DAG when evidence
triggers a re-plan — never to weaken the frozen protocol.

**Inputs**: completed mission outcomes; current DAG snapshot; evidence graph;
remaining budget and remaining revision quota.

**Outputs**: a list of new mission nodes (same shape as `research_plan.missions`
entries) to append. Each new node id must be unique and must not collide with a
`locked_id`.

**Triggers (declare which fired)**: evidence conflict; repeated failure of a
line of attack; budget drift; insufficient statistical evidence; tool/data
failure. If no trigger fired, return an empty list.

**Steps**
1. Read the completed outcomes and evidence graph; identify the trigger.
2. Choose catalog missions that close the gap (e.g. an extra ablation, a
   robustness re-run, a leakage audit, a new baseline).
3. Wire `deps` so the additions run after the evidence they depend on.
4. Never touch locked ids; never relax an endpoint, holdout rule, or budget.

**Failure modes**: unbounded re-planning; adding a mission that re-tests a
sealed holdout; proposing to change a frozen endpoint.

**Stop**: a bounded, dependency-correct set of additions, or an empty list.
