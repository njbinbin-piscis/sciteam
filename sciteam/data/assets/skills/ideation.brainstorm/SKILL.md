# Skill: ideation.brainstorm

## Purpose
Move from vague curiosity to a short list of defensible research directions
using structured ideation lenses — not free-form “brainstorm vibes”.

## Provenance
Distilled from Orchestra Research `brainstorming-research-ideas` (MIT):
problem-/solution-first framing, abstraction ladder, gap articulation.
Rewritten for SciTeam missions (falsifiable hypotheses + frozen eval binding).

## Requires capabilities
- literature.search

## Inputs
- Mission topic / charter bounds
- Optional prior lit_map or claim cards

## Outputs
- `idea_cards[]`: each with `mode` (problem_first|solution_first), `statement`,
  `gap`, `who_needs_it`, `falsification_hint`, `grounding` (tool-backed or
  `unverified:`)

## Steps
1. Confirm literature.search is available; retrieve 3–8 anchors before claiming novelty.
2. Write each candidate in one sentence; classify problem-first vs solution-first.
3. For problem-first: name who suffers and how much; for solution-first: name ≥2 real problems it addresses.
4. Run the abstraction ladder (up / down / sideways) once per promising card.
5. Drop cards that cannot bind to a measurable outcome or frozen-eval interface.
6. Never invent papers to justify novelty — use tools or mark `unverified:`.

## Failure modes
- Solution looking for a nail; novelty claimed from memory; unfalsifiable slogans.

## Stop
3–7 idea cards, each with gap + falsification hint + grounding status.
