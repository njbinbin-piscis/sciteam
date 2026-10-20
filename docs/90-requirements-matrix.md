# 90 · 需求矩阵

> 论文中的「完备性」只相对于本矩阵声明：每一项都要有机制（代码位置）、测试（自动化）和真实证据（运行记录）。未完成的项如实标注，不写入论文结论。
> 状态：`设计` → `实现` → `测试通过` → `有真实证据`。

## H · Harness（第一层）

| 编号 | 需求 | 机制 | 测试 | 证据 | 状态 |
|---|---|---|---|---|---|
| H1 | 每次激活都有明确结局 | `harness/loop.py` `_finish`、`finish` 工具 | 无 finish 纯文本 → `error(no_finish)` 等场景测试 | Verified flash 500/500 有 `activation_finished`，最终均为 completed；smoke20 20/20；TB 结局回填 Harbor | 有真实证据 |
| H2 | 长会话不因上下文溢出失败 | `harness/compaction/` 触发、切点、摘要 | 50 轮压缩集成测试、摘要失败回退测试 | dev 集 61 题对照（`runs/dev61s_*`，已清理 git 历史）：400K 触发线 0 次压缩、39/61；128K 18 次、40/61；64K 179 次、41/61。按题配对差值在 ±6 题以内，压缩没有拉低解决率。Verified 500 题本身未达 400K 线 | 有真实证据 |
| H3 | 压缩可审计、可取回 | `compaction_finished` 事件、`recall` | recall 取回原文 | 64K 组 179 次、128K 组 18 次压缩，均无失败。TB 自然触发 1 次（`winning-avg-corewars__2NVhK4J`，404788 token，摘要写入事件）。该次没有调用 `recall`；取回原文由单元测试覆盖 | 有真实证据 |
| H4 | 预算按真实费用计量且对模型可见 | `harness/budget.py`、`providers/llm/pricing.py` | 三档阈值通知、收尾调用 | Verified flash 每题费用（合计 $28.68）与 288 条周期状态通知；70%/85% 阈值通知未在真实运行中触发（正常任务碰不到断路器，符合 00 §3） | 有真实证据（计量与状态通知） |
| H5 | 供应商错误可恢复 | `loop.py` `_call` 重试退避；`QuotaExhausted` 熔断 | 各错误类别注入、402 分类 | Verified flash 中 1 次 transient 重试后成功；2026-09-24 真实余额耗尽（402）暴露分类缺陷，已修复并有测试，受影响的 243 题已重跑 | 有真实证据 |
| H6 | 崩溃后可续跑 | 事件折叠恢复 `Harness.resume` | 崩溃续跑、飞行中调用标为结果未知 | `runs/layer1_h6h7`：真实模型写完第一条工具结果后进程以 99 退出（尾事件 `tool_finished`，无 `activation_finished`）；续跑写出 `activation_resumed` 并以 completed 结束。基准驱动仍对崩溃题整题重跑（20 §3），不走这条路径 | 有真实证据 |
| H7 | 可离线重放 | `RecordingLlm` / `ReplayLlm` | provider 级回放；整次激活回放（请求逐次哈希一致、事件序列与载荷一致）`test_whole_activation_replay_is_consistent` | 同上一次续跑录下 1 次真实模型调用，`ReplayLlm` 离线重放后续事件序列与载荷一致，结局同为 completed | 有真实证据 |
| H8 | 单智能体能力达标 | 以上全部 | — | Verified 500 × flash：修正构建产物后 SciTeam 414/500（82.8%），官方 79.0%；同规则 pi 415/500、Hermes 414/500。TB2.1：主作业加补跑 361/428（84.3%），官方参考 82.7%。pro 实验已取消 | 有真实证据 |

## C · 通信（第二层）

| 编号 | 需求 | 机制 | 测试 | 证据 | 状态 |
|---|---|---|---|---|---|
| C1 | 私聊 | `comm/service.py` `_plan_post_message`（`dm:<a>+<b>` 会话）、`can_read` | `test_dm_is_private`：非成员不可读；私聊中 mention 第三方被拒 | 10 次 minipkg 真实运行（5 策略 × flash/pro，`runs/team/*`）：私聊 0–38 条/次，非成员写入 0；越权读取他人私聊被拒（如 flat `@lead cannot read this private conversation`）；私聊中 mention 第三方被拒（`probe_pingpong_1`） | 有真实证据 |
| C2 | 聊天室与线程 | `#room`、`reply_to` → 扁平线程根；策略 `rooms` 预置 | `test_room_threads`、`test_mention_of_nonmember_joins_room_but_not_dm` | 同上 10 次：房间/任务会话消息每次都有，线程回复 0–8 条/次（workflow 两次与 wave pro 为 0；workflow 策略不允许 peer 在房间发言） | 有真实证据 |
| C3 | 提及唤醒 | `runtime/attention.py` `attention_for` + 策略 `wake_on`；`scheduler.py` debounce 与激活中送达 | `test_mention_wakes_only_mentioned`、`test_debounce_batches_messages`、`test_message_during_activation_is_delivered_in_place`、`test_long_activation_does_not_block_others`、`test_concurrency_cap` | 10 次运行共 193 次激活，无注意项的激活 0；对 agent 的 mention 全部送达（未送达 0），各运行送达延迟中位 2.7–19.5 s（debounce 5 s），唤醒空闲者的最大延迟 6.0 s；超过此值的全部是对方正在激活中、于其下一次模型调用前就地送达（`incoming_added`），最大 214 s（market pro，受当前步骤时长约束）；定向探针 `probe_mention_1`：人类在三人房间 mention `@b`，只有 b 被激活（a、c 各 0 次） | 有真实证据 |
| C4 | 任务状态机 | `comm/board.py` 迁移表、FS/SS/FF/SF、环检测、CPM；`task_ready`/`task_open` 唤醒 | 合法/非法迁移、四种依赖、环、级联取消、并发认领单赢家、CPM 教科书例题；`test_dependency_readiness_wakes_assignee` | 10 次运行共建 88 个任务，全部 done；出现 assign/claim/start/submit/approve/reject 各迁移；非法迁移（如未 start 就 submit）被结构检查拒绝并由 agent 自行更正 | 有真实证据 |
| C5 | 通信方式由配置决定 | `configs/comm/{flat,hierarchical,workflow,wave,market}.yaml`，选择器语言 + `PolicyGate` | `test_policy_matrix`（五策略允许矩阵）、`test_hierarchical_reachability`、`test_workflow_policy_denies_peer_messages` | 五策略通信图各不相同且符合策略：hierarchical 私聊呈以 lead 为中心的星形，策略拒绝 6–7 次（如 `dev3 may not mention @rev`）；workflow 除向 owner 汇报外无私聊；market 全部任务经 claim 领取。`check.py` 重放审计：10 次运行共 701 个 Act 在新 CommService 上重放，决定与领域事件 100% 一致，无因果的领域事件 0；换用 flat 策略重放 hierarchical 运行即出现不一致（审计有辨别力） | 有真实证据 |
| C6 | 静默检测与限流 | `scheduler.py` `quiescent`、消息速率（`PolicyGate.rate`）、每小时激活上限、连续 handoff 上限、风暴与预算断路器 | `test_rate_limit`、`test_storm_breaker`、`test_timer_keeps_run_alive_and_wakes`、`test_handoff_continues_and_error_faults`、`test_resume_unfinished_activation_after_crash`、`test_stale_timer_does_not_hold_finished_run`、`test_second_scheduler_on_same_run_is_refused` | 10 次 minipkg 运行全部以 `run_quiescent` 结束，无断路；探针：无限计数游戏中 agent 自行约定停止（`probe_pingpong_1`）；私聊对限流收紧到 5 条后被拒并停下（`probe_pingpong_pairlimit`）；无进展阈值 6 时风暴断路器在第 7 条触发并停止运行（`probe_pingpong_storm`）。真实运行暴露并修复两个缺陷：过期定时器阻止静止、早先一次进展让之后闲聊免检 | 有真实证据 |

## I · 制度（第三层）

| 编号 | 需求 | 机制 | 测试 | 证据 | 状态 |
|---|---|---|---|---|---|
| I1 | 所有外部作用经单一 Gate | `CommService.submit` + `InstitutionGate`（`extra_gate`）；制度 Act 走同一 submit（40 §1、§6） | 源码检查：只有两个 service 写领域/制度事件（`test_institution_runtime.py` AST 检查）；折叠重建一致 | 3 次真实运行（`runs/team/audited_1`、`audited_pro_1`、`legislate_1`）无因果的领域事件 0；`check.py` 仅凭外部输入（genesis、初始规则集、验证结果）把 346 个 Act 重放到新制度实例：audited 两次决定 0 不一致，facts、账本、规则版本全等；legislate 跨 3 个规则版本重放，唯一 1 处不一致是修复 gate 缺陷（validate 未蕴含 evidence，见路线图第三层第 5 条）前记下的那次决定，属预期 | 有真实证据 |
| I2 | 制度事实由规则产生，不由模型判定 | counts-as 规则（40 §8）+ 机械验证器（§7） | 验证失败时不产生事实；reopen 撤销事实；`children_have` | audited flash/pro：done 任务 8/6 个，与 `accepted` 事实一一对应，每个都有通过的验证记录；legislate 初始规则无 facts，15 个 done 任务不产生事实（规则决定，而非模型） | 有真实证据 |
| I3 | 规则可版本化与修订 | 规则集版本、修宪程序（40 §2、§9），通信策略同样可修 | 投票、撤回、过期、冲突、受保护规则（含掏空验证器）、通信策略修正案、版本戳 | `legislate_1`：委员会（lead + rev）提 A1 后撤回；A2（submit/approve 须过 `tests`）、A3（每次提交须带自己的验收命令）经对方投票通过并生效，版本 d8543809 → 26c29e1e → 737f3be9；之后的决定按新版本（新规则 `audited-submit`/`audited-approve` 拒绝 4 次）；lead 与 rev 各自用红提交测试新规则，被 V3/V5 失败结果拒绝。修宪由 owner 定向消息触发（探针，非自发）；此前委员会用约 27 次不合 schema 的提案摸索规则词汇，已在 `rules` 工具中加入词汇表 | 有真实证据（定向探针） |
| I4 | 人类审批点 | `approvals` → pending → `decide_pending`（40 §6.2）；`sciteam team decide` | 人类签字/拒绝；未批准不产生领域事件；等待中重启 | audited flash/pro：发布任务 T1 在 rev 批准后停留 in_review，直到 owner 用 `team decide` 批准（flash seq 2888 → done 2890；pro seq 3107 → done 3109），签字前的检查另存为 `check_presignoff.json` | 有真实证据 |
| I5 | 有产出必审计 | `requirements`：submit 需证据提交 + 验证器通过；四眼原则（40 §4–§7）；验证在干净导出的提交上、无网络沙箱内运行 | pending → 验证 → allow/deny；等待中世界变化时 deny；无 commit 的 validate 直接拒绝 | audited flash 18 次验证全部通过（1 次缓存命中）；audited pro 16 次验证中 2 次真实失败，`submit-validated` 拒绝提交，agent 修复后重交通过；legislate 9 次验证 3 次失败被拒，1 次基础设施失败（因上述缺陷）未扣分 | 有真实证据 |
| I6 | 奖惩记账 | `ledger` 规则（40 §8），进入简报与 `standing` | 账本规则匹配；验证失败扣分；重放一致 | audited pro：2 次失败验证各扣 2 分（`red-build`），交付 +10 × 6、评审 +3 × 6，终余额 dev1 8、dev2 10、dev3 8、lead 23、rev 25；重放账本全等。legislate 规则无账本，余额为空（奖惩可选） | 有真实证据 |

## O · 组织（第四层）

| 编号 | 需求 | 机制 | 测试 | 证据 | 状态 |
|---|---|---|---|---|---|
| O1 | 创始者依据宪章建立组织 | 创世包；manifest 为可修宪文件，`human_ratify` 批准后 `org_enacted`（50 §3） | 未批准的 manifest 不生效：对新席位的 hire 被拒 | 从使命到组织的完整运行 | 有真实证据：5 次创世运行由创始者提出章程，所有者批准后 `org_enacted`；自发修宪 1–6 次（91 第四层） |
| O2 | 岗位配备 harness | `hire`：模板复制与改写入账、席位 harness 白名单；无匹配时 staffing 任务（50 §4） | 超员/非法 harness/未知模板被拒；无匹配模板时申请流程走通 | 同上 | 有真实证据：每次运行 5–6 次 `hire`，模板 sha 与是否改写都已入账；flat1 中创始者先雇 HR，再由 HR 雇其余 5 人；market1 设了 HR 席位，但所有人都是创始者雇的 |
| O3 | 环境就绪有凭证 | `certify_environment` 运行 `env-*` 探针验证器；`{certificate}` 条件（50 §5） | 无证书不可 start 实验类任务；改探针后证书失效 | 同上 | 有真实证据：`starts_before_certificate` 全为 0；genesis1 改探针后证书失效并重新签发 |
| O4 | 目标有可判定的成功条件 | 目标 = 带验收命令的根任务；受保护规则；全部 accepted → `run_stopped{goal_met}`（50 §6） | 条件未满足不能关闭目标 | 同上 | 有真实证据：5 次运行都由 `goal_met` 停机，隐藏测试 73–74/74 |
| O5 | 五种协作模式只靠配置定义 | `configs/org/templates/<mode>/`（manifest + 通信策略 + 规则）（50 §8） | 同一代码加载五套模板并生效 | 两种模板真实运行；五模式对比在第五层 | 有真实证据：flat、market 两种模板真实运行（74/74）；五模式对比在第五层 |

## X · 演化与实验（第五层）

| 编号 | 需求 | 机制 | 测试 | 证据 | 状态 |
|---|---|---|---|---|---|
| X1 | 自变量只能是配置 | `run_started.config.interpreter_digest`；报告检查同一实验内摘要一致（60 §2） | 摘要对源码敏感、对配置不敏感；摘要不一致判为无效 | 每个实验报告中写明摘要 | 有真实证据：x-base-hidden、x-base-visible、x-inst-smoke 共 65 个 run 摘要相同（`e8a4e4022a17`），报告 VALID |
| X2 | 实验可声明、可续跑 | `configs/experiments/*.yaml`；`sciteam exp run`（60 §3.1–3.2） | 展开、交错、跳过已完成的 run | minipkg 冒烟 | 有真实证据：minipkg 冒烟、X-base 60 个 run、X-inst 五臂冒烟均无人工介入跑完 |
| X3 | 外部客观判分 | `Grader`：minipkg-hidden、commit0、swebench（60 §3.3） | commit0：参考实现约满分、挖空仓库约零分 | X-base 校准 | 有真实证据：15 个库的参考实现 ≥ 0.998，minitorch 0.065 已排除；X-base 60 个 run 都完成判分。babel 待核查（60 §4.1） |
| X4 | 指标只由事件与判分计算 | `closed`、`false_closure`、`stuck`、`recovered` 等（60 §5） | 合成事件流 | 各实验报告 | 单测 + 真实 run：X-inst 冒烟中 `false_closure`、`recovered` 按定义计出 |
| X5 | 审查、证据与内生验证机制在真实 agent 下的消融（Q1 / Q1a / Q1b） | X-inst 五臂覆盖层（60 §6.2）、X-inst-confirm + `x-abandon-pilot`（60 §6.7）、X-evid 双盲复算三臂（60 §6.6、§6.9） | 覆盖层各自只改一个机制；`evid_analyze.py` 重算与两层/按题聚类 bootstrap | X-inst 90 runs + X-inst-confirm 576 runs + `x-abandon-pilot` 10 runs + X-evid 432 runs | **有真实证据**：Q1（90/90 有效，无独立证据时审查无效）→ Q1a（576 runs + 10 runs，留出证据将已关闭均分从 0.562 提至 0.996，赋予角色取消权将死循环从 90% 降至 30%）→ Q1b（432/432 完成，431 有效 + 1 断路器，双盲复算显著削减 39%–47% 虚假关闭、首轮精确率 +10.7%，并锁定对称闸门 `(1,0)` 损耗与步骤名锚定定律） |
| X6 | 五种模式对比 + 单 agent（Q2） | X-mode（60 §7） | — | 224 个 run | 设计 |
| X7 | 修宪驱动的跨任务演化（Q3）与 co-scientist 干实验（Q4） | `exp carry`；X-sci v0.2（60 §8–9） | carry 只导出修宪产生的文件 | X-evo 两臂各 2 条链；X-sci 待登记 | 设计 |
| X8 | 人机混合组织的注意力路由与裁决制度（Q6）及人类席位操作台 | `X-human` 四臂覆盖层（60 §14）+ `sciteam_web` 双平面操作台（`70-web.md` W1–W4） | 人类动作经同一 `CommService.submit` 入账且 `check.py` 重放一致；注意力遥测指标重算 | Track A（288 runs 受控代理 PI）+ Track B（36 runs 真人经 `70-web` 盲测） | 设计（60 §14、`70-web.md` v0.1） |
