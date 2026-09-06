---
id: skill.revise
version: "1.0.0"
---

# Skill: skill.revise

## Purpose
Propose a revision to an existing skill after a campaign produces evidence that
the skill's steps, failure modes, or stop conditions are wrong or incomplete.

This does **not** auto-merge into `assets/skills/`. It writes a proposal for
operator review (self-evolution is gated).

## Inputs
- Skill id to revise
- Evidence: mission logs, tool_trace, integrity failures, negative results
- Proposed patch (markdown diff or replacement sections)

## Outputs
- `skill_proposals/<skill_id>_<timestamp>.md` under the run directory
- Facts summarizing why the current skill failed the operator

## Steps
1. Identify which skill step failed (cite mission id + log excerpt).
2. Draft a minimal patch: purpose/steps/failure modes/stop only — no brand names.
3. Build an evidence bundle from persisted agent/tool/inner-loop/assessment/KB
   events. Every proposal claim must cite a real `path#L<event>` reference.
4. Write only through the proposal interface under
   `runs/<run>/skill_proposals/`; never write `assets/skills/`.
5. Include proposed version, provenance, applicability scope, replacement
   content, and regression expectation.
6. Do not claim the skill pack has changed. A named human must approve it into
   one experiment pack before a later run can load the new version.

## Failure modes
- Vague "improve the skill" without evidence pointers
- Proposing unrelated new skills as a substitute for fixing the broken one
- Citing an event or path absent from the generated evidence bundle
- Generalizing a one-run result to every domain or paradigm

## Stop
Proposal file written and summary cites evidence; waiting for human merge.
