# 委员会简报：如何用 ISA 设计「假设锦标赛」制度

> 给 `m_rules` / planner / 人类立法者。完整原语见 [`../../ENGINE_ISA.md`](../../ENGINE_ISA.md)。  
> 技能卡：[`../skills/institution.role_pipeline/SKILL.md`](../skills/institution.role_pipeline/SKILL.md)  
> 本简报含 **2026-07-19 live 验收**（不只是 fake）。

## 不要做的事

- 不要只注册六个角色名却设 `max_parallel: 6` 齐射——那是假锦标赛。  
- 不要要求引擎增加 `Phase.GENERATION` 之类枚举。  
- 不要让每个岗都能写 `candidate_hypotheses.json`。  
- 不要把「ISA / 制度验证」写成每张任务卡的 Goal 置顶——工人会不知道本席交付物。  
- 不要用 Observatory 战役 UI 的 `summary.json` 缺省去判断 harness 跑次「没进度」；看 `isa_result.json` + board。

## 要做的事（最小配方）

1. **波内阶段图** → `coordination.work_graph.role_pipeline`（仅生产岗；不含裁决席）  
2. **出仓岗** → 唯一成员 `emits_exit_artifact: true`（通常 meta_review）  
3. **裁决席** → 另设 `authority: [may_assess_round]`（与 emit 互斥）；结项/停滞由其按章程裁决  
4. **空转阈值** → `charter.stagnation_max_rounds`（原文进事实包；由裁决席运用，非引擎 if）  
5. **波间汇合** → `scheduling.mode: barrier`（与 pipeline 叠加；`max_parallel` 只是容量）  
6. **契约** → `exit_contract` + `artifact_gate`（gate 观察值进事实包，裁决席判是否满足）  
7. **职责优先提示** → 每席 `task_brief`（L2）；前驱输出经 heartbeat 注入  
8. **非 emit 席** → 不要灌完整 exit JSON Schema；工具轮次宜封顶（防文献空转）

参考资产：`assets/teams/paradigms/co_scientist_tournament.yaml`  
验收脚本：`harness/run_isa_hyp_tournament.py --mode fake|live`

## 验收判据

| 判据 | 合格 | 不合格 |
|---|---|---|
| 同波认领顺序 | 跟随 role_pipeline | 六岗几乎同时 claim |
| 终稿文件 | 仅 emit 岗写入后出现 | 永远 draft 或无人写 |
| 空转 | ≤ stagnation 轮 fail-honest | 数十/百轮 CONTINUE |
| E0 | 零引擎科学相位改动 | 为锦标赛改 orchestrator 分支 |
| 甘特 | 成员条按 FS 切片 | barrier 下全员条重叠满格 |
| 简报 | mission 进度 / outcome 非空 | harness 跑完仍 0/0「尚未结项」 |

## Live 证据（务必复用）

> 本节原引用的 `..._live_20260719T042128Z` 已在 2026-08-07 实验系统整理中
> 被清理（早期跑，未被 `INCLUDE_RUNS`/论文引用）；换成仍存在、已复核的
> 下列运行，数字随之更新。

- **Run**：`runs/isa_hyp_tournament_live_20260731T011057Z`
- **结果**：`status=completed`，`contract_ok=true`，`rounds_used=1`，墙钟 ≈ 15 min，29 次 LLM
- **顺序**：generation → reflection → ranking → evolution → proximity → meta_review
- **出仓**：`missions/m_hyp_isa/candidate_hypotheses.json`（3 candidates + rejected_ideas）
- **课题**：serial pipeline 上 early-vs-late error compounding（非湿实验）

下一次完整战役要把上述配方写进 `m_rules` / 范式选择，而不是再发明一套相位枚举。
