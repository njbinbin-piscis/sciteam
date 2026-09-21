# SKILL: topic.select

## Purpose
Score the registered problem pool and pick the working problem.

## Requires capabilities
- literature.search

**Inputs**: lit_map entries; the frozen protocol's problem pool.

**Outputs**: `problem_choice`: chosen id, per-problem scores, rationale.
`scores` maps each problem id to a number, or to an object whose values are
all numbers (named criteria breakdown) — no strings or nesting deeper than one
level.

**Steps**
1. Score each pool problem 0–1 on: evidence richness (survey support),
   mechanism clarity (can we state a falsifiable hypothesis today?), and
   frozen-eval loop cost (cheap iterations beat prestige).
2. The choice must be a pool member — the pool is frozen; no write-ins.
3. Rationale references specific lit_map entries.

**Failure modes**: choosing by topic glamour; scores invented without survey
support.

**Stop**: one chosen problem with defensible scores.
