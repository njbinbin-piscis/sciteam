# rules.legislate

Enact or revise `campaign_rules` for this campaign under GRUNDNORM G3.

## Must

- Reference `assets/institutions/GRUNDNORM.md` as `grundnorm_ref`
- Include `allowed`, `forbidden`, `audit_matrix`, `scoring`, `authors`, `red_team`
- Ensure every promotable artifact kind appears in `audit_matrix`
- Prefer `scoring.mode=record_only` unless the operator brief asks for soft/hard
- Append intent to `institutions/ledger.jsonl` (harness may also append)

## Must not

- Contradict GRUNDNORM (no engine forks; no success=true-as-completion)
- Skip red-team field (`no_veto` or `veto_with_reasons`)
