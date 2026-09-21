# 开源科研 Skill 调研与采纳（SciTeam Lab）

> 调研日：2026-07-13。目标：把**可迁移的科研流程规范**收进实验平台，
> 而不是搬运框架操作手册或未接线的生物信息学工具包装。

## 1. 主要来源

| 来源 | 规模 / 取向 | 对本平台的价值 |
|---|---|---|
| [Orchestra-Research/AI-research-SKILLs](https://github.com/orchestra-research/AI-research-SKILLs) | ~98；AI 研究全生命周期 + 工程栈 | **高**：Ideation / ML Paper Writing / ARA Rigor Reviewer |
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) | ~148；生信/化学/数据库工具技能 | **中低（现阶段）**：多为具体库/API 配方，缺对应 Port 时采纳会诱导空转 |
| [jaechang-hits/SciAgent-Skills](https://github.com/jaechang-hits/SciAgent-Skills) | ~199；组学流水线 | **低（现阶段）**：同 K-Dense，偏执行环境配方 |
| [agentskills/agentskills](https://github.com/agentskills/agentskills) | 标准本身 | **格式参考**：`SKILL.md` + frontmatter + progressive disclosure |
| Anthropic bio-research plugin | MCP 聚合 + 问题选择框架 | **理念**：问题选择/文献工具绑定；不直接拷贝闭源 MCP |
| deep-research / evidence-first skills（社区） | 证据账本、矛盾检查、不确定度 | **高**：与 Line C「证据可审计」同构 |

## 2. 采纳原则（硬过滤）

1. **只采纳流程与认识论**，不整库拷贝工具配方（Megatron/vLLM/Scanpy…）。
2. 采纳项必须能挂上本平台已有/计划中的 **capability**（检索、审计、冻结评测）。
3. 每条技能必须声明 `Requires capabilities`；缺能力则预检失败。
4. **注明灵感来源**，正文用 SciTeam 契约语言重写（exit contract / verifier / KB），禁止 ARA/Claude Code 路径耦合。

## 3. 采纳映射

| 开源要点 | 本平台技能 id | 决策 |
|---|---|---|
| Orchestra `brainstorming-research-ideas`（问题/解法优先、抽象梯子、张力对） | `ideation.brainstorm` | **采纳** |
| Orchestra `creative-thinking-for-research` | `ideation.tension_hunt` | **采纳**（张力/矛盾猎取） |
| Orchestra `ara-rigor-reviewer` 六维认识论评审 | `review.epistemic_rigor` | **采纳**（改挂 SciTeam 产物，非 ARA 目录） |
| Orchestra `ml-paper-writing` 方法/声明边界 | `paper.methods_reporting` | **采纳** |
| deep-research：claim↔evidence、反证、不确定度 | `evidence.claim_support` | **采纳** |
| 可复现清单（多源共识） | `repro.audit` | **采纳** |
| K-Dense / SciAgent 组学·化学工具 skill | — | **暂缓**（无 Port / 易幻觉） |
| Orchestra 分布式训练 / 推理栈 skill | — | **不采纳**（工程手册，非科研制度） |
| Autoresearch 总编排 | — | **不采纳为技能**（本平台已有 Campaign/Mission 编排） |

## 4. 与岗位目录的关系

技能是**岗位能力包**；组队时优先从 `assets/roles/CATALOG.yaml` 选岗，
再绑定技能 allowlist。禁止 `worker` / `stage_*` 之类无岗位语义的名字。
