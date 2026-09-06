# SKILL: domain.algo.repro_seed

**Purpose**: test claim robustness across seeds (adversarial standard tool).

**Inputs**: candidate module; frozen eval script (supports `--seed`).

**Outputs**: per-seed verdict table; robustness conclusion.

**Steps**
1. The registered seed's result is the official one; alternative seeds probe
   robustness only.
2. Run ≥5 alternative seeds; record predicate outcome per seed.
3. Gain that appears only on the registered seed = seed-tuned artifact →
   report as `weakened`/`falsified` in the attack report.

**Failure modes**: cherry-picking friendly seeds; averaging away a failure.

**Stop**: table complete; conclusion follows the table.
