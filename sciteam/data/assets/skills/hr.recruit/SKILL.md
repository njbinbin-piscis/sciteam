# Skill: hr.recruit

## Purpose
Institutional HR desk: create, recruit, and train **agent instances** for this
experiment pack. System roles/skills under `assets/` are templates only;
missions must use pack instances staffed through this skill by the dedicated
`hr_officer` agent (LLM tool loop when a client is bound).

## Requires capabilities
*(none — filesystem pack operations; no external evidence tools)*

## Tools (HR session)
- `list_role_templates` — catalog of system role templates
- `list_pack_agents` — instances already in this experiment pack
- `inspect_agent` — read one pack agent instance + bound skills
- `recruit_agent` — clone/train a pack instance from a template

## Inputs
- Mission staffing request: paradigm member keys / specialty needs
- Optional template post id from `assets/roles/CATALOG.yaml`
- Optional train_notes (mission-specific adjustments)

## Outputs
- `runs/<id>/pack/agents/<agent_key>/AGENT.md` (instance)
- Copied `pack/skills/<skill_id>/SKILL.md` for bound skills
- `pack/hr_log.jsonl` recruitment event
- `pack/hr_sessions/<mission_id>_<HHmmss>/` observable session:
  `request.json`, `transcript.jsonl`, `tool_trace.jsonl`, `staffing_report.json`

## Steps
1. Read the specialty need; pick the closest catalog template (never `worker` / `stage_*`).
2. Call tools to inspect templates / pack; then `recruit_agent` for each seat.
3. Clone template prompt → pack agent instance; attach `default_skills` (and extras if needed).
4. Adjust train_notes for this campaign only — do not mutate system templates unless an operator explicitly edits templates in the console.
5. Emit a staffing JSON envelope (`summary` / `roster` / `rationale`); refuse generic identities.

## Failure modes
- Staffing with `worker` / `stage_a` names
- Skipping pack write and “remembering” a role
- Editing only the live prompt without an instance file
- Claiming seats without calling `recruit_agent` (gate may auto-complete, but the session must still be honest)

## Stop
Every mission seat has a pack agent instance with skills listed in frontmatter;
the HR session artifacts under `pack/hr_sessions/` are complete and inspectable.
