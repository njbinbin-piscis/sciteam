# Skill: wizard.intake

## When to use
Operator is creating a new SciTeam campaign and needs a ResearchPlan before launch.

## Procedure
1. Load theory constraints from `assets/wizard/SYSTEM.md`.
2. Use **ask_question** (`sciteam/ask_question.py`):
   - Prefer `text_input` for research goals and seed-plan config gaps.
   - Use `quick_pick` / `multi_select` only as accelerators, never as the only path.
3. Understand free-text replies: update intake state; ask only for **gaps**.
4. **Never** emit `plan_draft` until agent audit + structural checks pass.
5. Emit schema-valid `research_plan`; operator confirm is a separate action.
6. Launch path runs `validate_launch` — incomplete goals/plans are hard-rejected.

## Anti-patterns
- Running wizard / campaigns without `SCITEAM_LLM_*`.
- Hardcoded type/budget-only questionnaires as the product UX.
- Treating historical template packs as mandatory project kinds.
- Drafting or launching with placeholder goals.
- Claiming wet-lab success without an external verifier path.
