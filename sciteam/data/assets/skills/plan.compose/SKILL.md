# SKILL: plan.compose

**Purpose**: compose a schema-valid `ResearchPlan` (problem choice + mission
DAG) from the charter, the frozen candidate pack, the literature evidence graph,
the mission/tool catalog, prior failures and the budget.

**Inputs**: charter bounds; `candidate_pack.yaml` (frozen problems); evidence
graph digest; catalog of missions/paradigms/tools; prior mission outcomes; the
campaign budget.

**Outputs**: `research_plan.json` conforming to `research_plan.schema.json`:
`problem_choice` (a pack member, with literature-grounded rationale),
`hypotheses`, `missions` (DAG nodes with `deps` and `on_fail`), `locked_ids`,
`risks`, `fallback`, `budget`.

**Steps**
1. Choose the working problem from the frozen pack only — no write-ins. The
   rationale must cite located literature spans, not search snippets.
2. Draft falsifiable hypotheses with a stated falsification criterion.
3. Build the mission DAG from catalog missions/paradigms. Every claim-bearing
   mission must have a verifier-owned exit contract and, where relevant, an
   `artifact_gate`. Wire dependencies so evidence flows forward.
4. **Compose each mission team via HR (`hr_officer` / `hr.recruit`).**
   Prefer posts from `assets/roles/CATALOG.yaml`; bind `skills_allowlist` from
   those posts' `default_skills` (and adopted skills such as `ideation.*` /
   `review.epistemic_rigor` / `evidence.claim_support` when relevant).
   System assets are templates; the campaign uses **instances** under
   `pack/agents` and `pack/skills`. **Forbidden**: roster identities `worker`,
   `generic`, `stage_a`/`stage_b`/`stage_c`, or any other anonymous reusable
   label. If the task needs a specialty not in the catalog, ask HR to recruit a
   *professional* snake_case post — do not fall back to a generalist.
5. Mark `locked_ids`: the sealed-holdout audit, frozen primary endpoint and
   statistical-budget missions may never be revised away.
6. State risks and a fallback (including an honest-negative path).
7. Keep the plan inside the charter budget; set `max_revisions` conservatively.

**Failure modes**: inventing a problem outside the pack; a claim mission whose
results are worker-written rather than verifier-owned; unlocked holdout; a DAG
with no honest-negative fallback; meta-planning that exceeds budget; using
generic/stage agent names.

**Stop**: one schema-valid ResearchPlan whose DAG the AdaptiveCampaignPlanner
can execute unchanged.
