# Skill pack index

See `_SOURCE_ANALYSIS.md` for open-source skill survey and adoption decisions.
Each skill directory must contain `SKILL.md` with: purpose, inputs, outputs,
steps, failure modes, stop conditions.

**Requires capabilities** (mandatory when the skill depends on tools/ports):
declare a `## Requires capabilities` list (ids from `assets/tools/catalog.yaml`).
Mission start preflights these against `RuntimeCapabilities`; missing → fail closed.

**Team composition**: pick posts from `assets/roles/CATALOG.yaml`. Never roster
`worker` / `stage_*` / other generic identities.

## Planning (meta)
- plan.compose
- plan.revise
- rules.legislate
- wizard.intake — 新建项目向导（产出 ResearchPlan，不执行战役）
- skill.revise — 跑后技能修订提案（人工合入，非自动改包）
- hr.recruit — HR：从模板招聘/培训实验包内 agent 实例（组队必经）
- institution.role_pipeline — 波内 FS 阶段图（锦标赛/委员会；非引擎 Phase 枚举）

## Ideation (adopted from Orchestra ideation)
- ideation.brainstorm
- ideation.tension_hunt

## Science process
- protocol.register
- topic.select
- lit.survey
- lit.extract_claims
- lit.citation_guard
- evidence.claim_support
- hypothesis.formulate
- hypothesis.refine
- design.algorithm
- design.ablation_plan
- impl.scaffold
- impl.debug_loop
- eval.run_frozen
- eval.compare_baseline
- adversary.attack
- review.epistemic_rigor
- repro.audit
- iterate.decide
- model.as_program
- institution.round_assess — 轮次裁决席（round_assessor 专用）

## Paper production
- paper.outline
- paper.related_work
- paper.method
- paper.methods_reporting
- paper.experiments
- paper.write_imrad
- paper.figures
- paper.checklist
- package.arm

## Harness evolution (line D)
- evo.propose — 提案席：从失败轨迹起草受限基因组 diff
- evo.verify — 核查席：held-out 清单机械复测，不改 diff
- evo.promote — 晋升席：只按核查账本晋升/回滚

## Organizational evolution (Grundnorm G7, line E)
- org.request_retro — 任一席位申请复盘（触发 org.retro，不自行改资产）
- org.retro — 战役复盘：机械记录 → retro_report（双环入口）
- org.amend_draft — 起草修正案（单案单资产，预测须可证伪）
- org.amend_review — 评审：必须调 amendment_audit 工具出账本
- org.shadow_verify — 按冻结 falsifier 读影子账本判定
- org.evolve — 仅限 governance:none 对照臂；直接改资产但台账照记

## Memory / context / ops
- memory.commit
- memory.recall
- context.compact
- budget.report
- gate.request_human

## Domain (algorithm)
- domain.algo.benchmark_io
- domain.algo.complexity_check
- domain.algo.repro_seed
