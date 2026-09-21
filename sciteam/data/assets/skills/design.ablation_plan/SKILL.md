# SKILL: design.ablation_plan

**Purpose**: plan the ablations that attribute any gain to its true cause.

**Inputs**: the design's components.

**Outputs**: `ablation_plan[]`: id, what is removed, the question answered.

**Steps**
1. One ablation per load-bearing component: remove it, keep the rest.
2. Each ablation names the question ("is tiering, not detection, the source
   of the gain?").
3. Include the degenerate ablation that reduces the design to the baseline —
   it must reproduce baseline numbers (sanity anchor).

**Failure modes**: ablations that change several things at once; skipping the
degenerate anchor.

**Stop**: every claimed effect has an ablation that could disconfirm it.
