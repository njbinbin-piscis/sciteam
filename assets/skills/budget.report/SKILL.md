# SKILL: budget.report

**Purpose**: report resource consumption against campaign/mission budgets.

**Inputs**: budget clock snapshot (missions, wall clock, tokens).

**Outputs**: one-line status per dimension + a recommendation
(continue / wrap up).

**Steps**
1. Report each dimension as used/limit (0 limit = unlimited).
2. Flag any dimension past 80% as amber, past 100% as red.
3. On red: recommend the wrap-up mission explicitly.

**Failure modes**: silent overrun; padding the report.

**Stop**: status delivered with one recommendation.
