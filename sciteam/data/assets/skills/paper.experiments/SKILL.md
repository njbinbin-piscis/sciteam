# SKILL: paper.experiments

**Purpose**: report frozen-eval results and ablations without spin.

**Inputs**: official results.json files; ablation results; attack report.

**Outputs**: experiments section + claim_result_map entries.

**Steps**
1. Every number in the section is copied (not recomputed) from a frozen
   results file and keyed to its result id.
2. Report the registered predicate verdict verbatim, then robustness
   (seeds) and ablations.
3. Negative/weakened findings get equal typographic standing with wins.

**Failure modes**: derived metrics ("2.3x better") without the underlying
frozen numbers; omitting failed ablations.

**Stop**: claim_result_map covers every quantitative sentence.
