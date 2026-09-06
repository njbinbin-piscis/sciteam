# SKILL: iterate.decide

**Purpose**: decide, after an eval verdict, whether to iterate locally,
escalate to the planner, or promote.

**Inputs**: eval verdict + gap analysis; mission budget remaining.

**Outputs**: decision record: `iterate` / `escalate_falsified` / `promote`,
with the single reason.

**Steps**
1. If the predicate passed: `promote` (to adversarial review, never straight
   to writing).
2. If failed but the gap shrank materially and budget remains: `iterate`,
   naming the one change to try next.
3. If failed with no plausible in-design fix, or budget is nearly exhausted:
   `escalate_falsified` — the planner decides what mission comes next; you do
   not silently restart the design yourself.

**Failure modes**: iterating forever on a dead design; promoting on a lucky
seed; hiding falsification from the planner.

**Stop**: exactly one decision with one reason.
