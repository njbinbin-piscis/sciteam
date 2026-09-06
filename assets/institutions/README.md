# Institutions layer — how G / C / L / P fit SciTeam

See **[GRUNDNORM.md](./GRUNDNORM.md)** for the frozen meta-rules.

## Mapping to runtime

| User intent | SciTeam locus | Status |
|-------------|---------------|--------|
| 1. 自我约束 / 诚实人格 | `assets/prompts/constitution.md` + role prompts | **已有**；叙述须对齐账本 |
| 2. 明确制度 + 产出必审 | `m_rules` → `campaign_rules` + `harness/institutions.py` 全 mission 审计 | **已接线** |
| 3. 奖惩记分 | `institutions/scorecard_final.json`（默认 `record_only`） | **已接线** |
| 委员会立法 | paradigm `planning_council` + bootstrap 立法（可 ledger 修订） | **bootstrap 已启用** |

## Artifacts per run

```
runs/<id>/institutions/
  GRUNDNORM.md          # pinned copy
  ledger.jsonl          # legislation / scorecard events
  audits/<mission>.json
  scorecard_final.json
runs/<id>/missions/m_rules/campaign_rules.json
```

## Tests

`PYTHONPATH=harness:. pytest tests/test_institutions.py`
