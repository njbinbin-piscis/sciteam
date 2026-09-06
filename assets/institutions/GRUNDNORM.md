# SciTeam Grundnorm — 制度底层原则（委员会不可废止）

> 状态：冻结层。任何 Planning Council / 制度委员会产出的二级规章，
> 若与本文件冲突，**一律无效**。引擎不得编码具体科研流程；本文件也不编码流程，
> 只编码「制度如何存在、如何被审计、如何被奖惩」的元规则。

## G0 · 分层

| 层 | 谁写 | 可变性 | 例子 |
|----|------|--------|------|
| **G · Grundnorm** | 人类操作员冻结 | 战役内不可改 | 本文 |
| **C · Constitution** | 人类（prompt 资产） | 战役间可修订，须记 three_questions | `assets/prompts/constitution.md` |
| **L · Legislation** | 委员会 agents | 战役内可立/改，须过立法原则 + 红队 | `runs/<id>/institutions/*.json` |
| **P · Practice** | 技能 / 最佳实践 | 可演进，须有审计挂钩 | `assets/skills/*` |

委员会可以立法，**不可以**修改 G；修改 C 视为人类侧修订，不是 agent 立法。

## G1 · 自我约束（人格与范式）≠ 唯一防线

1. 每个 worker 必须加载 Constitution（诚实、引用纪律、负结果合法）。
2. **提示词约束是软约束**：假定 agent 会在压力下美化叙述（phase7_lipo 已实证）。
3. 因此：凡影响结项、升迁、对外宣称的断言，**不得**仅依赖自我汇报。

## G2 · 权威账本与产出必审

1. **定量结果权威源**只能是声明的 verifier / 冻结脚本产物（或同等机检源）。
2. **凡有晋升资格的产出**（exit artifact、KB promote、对外 claim）必须经过 **审计工序**：
   - 结构审计：契约 / schema
   - 对齐审计：叙述中的数量句 ↔ 权威账本
   - 引用审计：citation 可解析或显式 `unverified:`
3. 审计角色与生产角色 **不得同一 agent_key 同时兼任**（可分身，不可同一身份既写又终审）。
4. 「能做什么 / 不能做什么」的清单属于 **L 层立法**；G 层只要求：清单必须存在、可机读、可审计。

## G3 · 立法原则（委员会制订制度时必须遵守）

委员会（默认 paradigm: `planning_council` 或专用 `rules_committee`）立任何二级规章时：

1. **不改装个体**：不得要求改模型权重或引擎代码；只许配置、契约、技能、门控、看板规则。
2. **可检验**：每条规则必须写明 *触发条件*、*合规判据*、*审计产物路径*。
3. **可失败**：必须声明诚实失败/负结果是合法终态之一（禁止把「科学打赢」绑死为「任务完成」，除非 G0 操作员显式覆盖）。
4. **最小必要**：能用既有契约/门控表达的，不新造平行制度；新造须在 three_questions 表登记。
5. **阳光化**：新规章写入 `runs/<campaign>/institutions/ledger.jsonl`（append-only），含作者、理由、废止指针。
6. **红队否决权**：立法案无 red-team 明确 `no_veto` / `veto_with_reasons` 记录不得生效。
7. **不与 G/C 冲突**：冲突检测由机检（关键词/指针）+ 红队双轨；冲突 → 无效。

## G4 · 奖惩（可选模块，默认开启记账、默认弱执行）

1. 每个 `agent_key`（含 replica）有 **integrity_score**（战役作用域，初始 0）。
2. **扣分触发（硬）**：叙述数量句与权威账本不一致；手写 verifier-owned artifact；伪造 citation 且未标 unverified。
3. **加分触发（软）**：首次提交即通过全套审计；诚实负结果被 package；有效红队否决阻止了一次错误 promote。
4. **考核时点**：mission 结束写 `scorecard.json`；campaign 结束汇总 `scorecard_final.json`。
5. **执行档位**（操作员在 brief 选择，默认 `record_only`）：
   - `record_only`：只记账，不影响调度
   - `soft`：低分 agent 降低 claim 优先级 / 禁止任终审审计岗
   - `hard`：低于阈值暂停 claim（须操作员或委员会解锁）

奖惩是 **可选强化**，不是真理来源；**账本与审计**才是。

## G6 · 人事编制（HR）

1. `assets/` 下的角色与技能是 **系统模板**；战役实际使用的是
   `runs/<id>/pack/{agents,skills}/` 下的 **实例**（可观察、可修订）。
2. **所有团队组建**必须经由专用岗位 `hr_officer`（技能 `hr.recruit`）：LLM 招聘轮
   调用 HR 工具从模板筛选、复制并培训实例；无 LLM 时写
   `deterministic_fallback` 会话（仍可观察）。禁止任务直接使用匿名/通用 agent
   （`worker`、`stage_*`）。
3. Mission 开跑前，名册必须解析为 pack 实例；HR 日志写入 `pack/hr_log.jsonl`；
   每次招聘会话写入 `pack/hr_sessions/<id>/`（transcript / tool_trace / report）。
4. 观察器「人才与技能」可查看 HR 会话行为与每个 agent 实例定义。

## G5 · 三问（引擎不变性）

任何新 L/P 制度上线前回答：

1. 是否纯模板/配置？  
2. 是否需要新 schema？  
3. 是否需要改引擎？ → **必须为 NO**（除非人类显式开 E0 例外战役）。

## G7 · 修订程序（Amendment Procedure）—— 2026-08-09 人类侧增补（线 E）

> 适用对象：对 C/L/P **持久资产**（`assets/` 或组织实例资产副本）的一切修改。
> 与 G3.5 的关系：G3 管战役内立法（`runs/<id>/institutions/ledger.jsonl`，战役结束即失效）；
> G7 管跨战役存续的资产修订。战役内立法若要固化为持久资产，必须走 G7。

1. **修正案义务**：一切持久制度修改必须以 `amendment_proposal` 契约工件提出，
   进入组织级修正案台账 `runs/_org/<org_id>/amendments.jsonl`；台账 append-only，属 G2 权威账本。
2. **分层门槛**：
   - **P 层**（技能/角色提示词）：提案 / 验证 / 晋升三席分立（proposer ≠ verifier ≠ promoter）。
   - **L 层**（范式/脊骨模板/角色目录）：受影响席位评审 + 影子运行 + 产出审计 + 红队记录。
   - **C 层**（constitution.md）：L 层全部程序 + 多席一致 + **人类门核准（真人逐案，经 budget_gate 通道）** + 同条款冷却期 ≥ 1 战役。
3. **G 层固化**：G 层不受理修正案。以 G 层资产或修订程序自身为目标的提案，
   preflight 必须拒收并记 entrenchment 事件。判层按路径白名单 fail-closed：白名单外路径一律拒收。
4. **可证伪义务**：修正案的 motivation 必须引用可解析的运行工件；prediction 必须可证伪，
   并由影子运行账本事后判定。对齐失败按 G2 产出必审原则处理。
5. **无治理例外仅限对照**：绕过本条程序改动 C/L/P 资产，仅允许出现在显式标注
   `governance: none` 的对照实验臂中，且同样必须落台账（`status: ungoverned_applied`）供事后审计。

---

*本文件是「制度的制度」。委员会的创造力用在 L/P；G 层保持极短、极硬、可证伪。*
