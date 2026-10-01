# 50 · 组织（Organization）

> 所属层：4。依赖 `kernel`、`comm`、`runtime`、`institution`。被第五层 `experiment` 使用。
> 状态：v0.1（2026-09-30）。

## 1. 原则

1. **组织是制度文件。** 组织 manifest（席位、分组、目标约定）与通信策略、规则文件一样，是规则集中的一个可修宪文件。「组织章程生效」就是一次修正案生效；不另开通道，不另写裁决逻辑。
2. **引擎不认识部门。** 代码里没有 HR、工程部、委员会这些名字。它们是 manifest 里的席位与分组，职责写在席位章程里；权限由通信策略和规则里的选择器授予（`role:<seat>`、`group:<name>`）。本层提供四个通用原语：**雇佣**、**解雇**、**环境证书**、**目标**。其中环境证书只是「验证器 + 一条事实」，放在第三层的条件词汇里（40 §5），本层只使用它。
3. **从一个创始人开始。** 运行开始时只有人类所有者、创始人 agent 和创世宪法。其余的一切（席位、通信方式、规则、人员、目标）都由创始人与后来的成员按宪法程序建立。
4. **员工是模板的实例。** `configs/agents/` 下的档案是系统级模板（`configs/profiles/` 留给第二、三层的固定团队，不改动，以免改变已有实验的提示词）。雇佣时，模板被复制并可按岗位改写，实例的全文写入事件（`member_registered`），所以运行包自带全部员工定义，重放不依赖模板目录。
5. **环境是公司资质，不是员工能力。** 镜像、工具、依赖由运营者提供（`31` §6）。工程岗的职责是用机械探针证明环境满足任务所需，并签发证书；探针不过就上报缺口，而不是让员工在缺条件时硬做。
6. **目标必须可判定。** 目标是带机械成功谓词（验收命令）的根任务；它是否完成由验证器判定，不由任何成员宣布。所有目标达成是运行的正常停机条件。

## 2. 规则集中的组织文件

团队文件新增 `org: org/<name>.yaml`（manifest）。它必须列在宪法的 `amendable` 中，才能在运行中修改；规则版本把它和其他规则文件一起计算（40 §2）。没有 `org` 时本层为空，行为与第三层完全相同。

```yaml
# org/<name>.yaml
mission: |                 # 进入每个成员的简报
  ...
seats:                     # 名字就是成员的 role
  - name: founder
    count: 1
    groups: [legislature, staffing]
    charter: |             # 进入该席位成员的简报；写职责，不写流程
      ...
  - name: hr
    count: 1
    team: ops
    reports_to: founder    # 雇佣时写入 Member.manager（取该席位的第一个在职成员）
    groups: [staffing]
    harness: [harness/team-institution.yaml]   # 该席位允许的 harness 配置
    templates: [hr]        # 推荐模板（只作提示，不作限制）
    charter: ...
default_harness: harness/team-org.yaml   # 席位未列 harness 时允许的配置
staffing: "group:staffing"               # 选择器：谁收到空缺通知
goals:
  label: goal              # 带此标签的根任务是目标
  fact: accepted           # 目标达成 = 该任务具有此制度事实
```

- 在职成员数不得超过 `count`。manifest 修正案若使某席位超员或删除有人在职的席位，整体加载失败，修正案被拒（先解雇，再修改）。
- `groups` 决定成员的 `attrs.groups`（逗号分隔）。manifest 生效时，本层对所有在职成员写 `member_updated`，使分组与 manifest 一致。
- `seats[].harness` 省略时，允许的 harness 为 manifest 的 `default_harness`。
- 规则集按路径前缀选择 schema（`institution/`、`comm/`、规则文件、`org/`）；`org/` 的 schema 由本层注册到第三层的 `FILE_KINDS`，没有注册的文件无法加载（重放审计必须 import 本层）。

### 2.1 选择器 `group:`

第二层选择器语言增加一个原子 `group:<name>`：成员的 `attrs.groups` 含该名字。它只读成员属性，第二层不知道分组从哪里来。宪法与规则因此可以写「立法者」「人事」而不预先知道席位名：

```yaml
amendment:
  proposers: "group:legislature"
  voters: "group:legislature"
```

## 3. 创世（O1）

创世包 = 团队文件 `teams/genesis-<x>.yaml`：

| 部分 | 内容 |
|---|---|
| 成员 | `owner`（人类）、`founder`（agent，role `founder`，`attrs.groups` 与 manifest 桩一致） |
| 通信策略 | `comm/genesis.yaml`：所有人可私聊、可 mention；可建任务（可修宪） |
| 宪法 | `institution/genesis.yaml`：原则；`amendable` 含 manifest、通信策略、规则文件；受保护规则见 §6；`proposers/voters: group:legislature`；`human_ratify: [org/genesis.yaml]`（只有组织章程需要所有者批准，规则与通信策略由立法者自行通过；`human_ratify` 可为 `true` 或路径列表） |
| 规则 | `institution/genesis-rules.yaml`：受保护规则 + 最少的初始规则 |
| manifest 桩 | `org/genesis.yaml`：mission（来自所有者）、只有 `founder` 一个席位 |
| 开场 | 所有者给创始人的私聊：使命与期望 |

流程（全部是已有 Act，没有专用编排）：

1. 创始人与所有者私聊澄清使命、成功标准、约束（需要时以 `need_input` 结束激活，等所有者回复）。
2. 创始人用 `org_templates` 查看组织模板（§8），选一个或自行起草，然后分别对 manifest、通信策略、规则文件 `propose_amendment`。
3. 立法者只有创始人时，提出即达到票数；manifest 的修正案等待所有者 `decide_pending`。**未批准的 manifest 不生效**：新席位不存在，对它的雇佣被拒（O1 单测）。
4. 批准后 `amendment_enacted` + `rule_version_activated`；本层随即写 `org_enacted{rule_version, manifest_sha, seats, vacancies}`，并向 manifest `staffing` 选择器命中的成员发空缺注意项。

manifest 与策略、规则分别修宪、分别批准，任一时刻的组织都由当时生效的文件定义，没有「半个组织」的中间态需要引擎处理：先生效的文件立即起作用。

## 4. 雇佣与解雇（O2）

### 4.1 Act

| Act | 参数 | 条件（本层结构检查；另有通信策略与制度规则） | 事件 |
|---|---|---|---|
| `hire` | `seat`, `member`（新 id）, `template`, `profile?`（改写后的全文）, `harness?`, `note` | 席位存在于**当前生效**的 manifest；有空缺；id 未被使用且合法；`template` 存在于模板目录；`harness` 在该席位允许列表中；profile 非空 | `member_registered{member, hired_by, seat, template, template_sha, profile_sha}`，随后按策略把新成员加入 `rooms`；新成员收到 `hired` 注意项（总是唤醒） |
| `dismiss` | `member`, `reason` | 成员在职且为 agent；不能解雇自己 | `member_removed{member, by, reason}`；其未结束任务 `unassign`（因果指向该 Act） |

- 谁能雇佣、解雇，由通信策略规则决定（例：`{act: hire, from: "group:staffing"}`）。本层不内置「只有 HR 能雇人」。
- `profile` 省略时使用模板原文；给出时工具返回与模板的 diff，并记录两者的 sha。这就是「培训」：从模板复制并按岗位改写。
- 模板目录 = `configs/agents/*.md`。每个模板的首行 YAML 头（`---` 包围）可声明 `title`、`fits`（适合的席位名）、`harness`。`profiles` 工具列出模板及其头部，`profile_show` 显示全文。
- 解雇之后该成员的激活、注意项全部停止；已写入的事件与账本保留。

### 4.2 无匹配模板

找不到合适模板时，不存在「自动生成」的引擎路径。按席位章程，staffing 组可以：

1. 自行改写最接近的模板（`hire.profile`），或
2. 建一个带 `staffing` 标签的任务，交给章程规定的席位（通常是 legislature 组），要求起草岗位档案；对方以任务产物（工作区中的档案文件 + 提交）交付，staffing 再以该文件内容雇佣。

第 2 条就是「向委员会申请」：它是普通任务，受制度规则审计。本层只保证雇佣的档案全文入账，来源（`template`、改写 diff、相关任务）可追溯。需要新的 harness 配置（不同压缩阈值、工具集）属于配置演化，在第五层处理；本层的 `harness` 只能从席位允许列表中选。

## 5. 环境证书（O3）

环境证书的机制属于第三层（40 §5 `environments` 段与 `{certificate}` 条件），这里说明它在组织中的用法。

### 5.1 定义

- 环境探针就是规则文件里的验证器（40 §4），命名惯例 `env-*`：在与团队相同的镜像、无网络、干净导出的提交上运行命令，看退出码。
- `environments` 段（规则文件，可修宪）把证书名映射到探针列表：

```yaml
environments:
  python-dev: {probes: [env-python, env-pytest]}
```

### 5.2 Act 与事实

| Act | 参数 | 条件 | 结果 |
|---|---|---|---|
| `certify_environment` | `env`, `commit`, `note` | `env` 在 `environments` 中；本层把它展开为 `{validate: [该证书的探针]}`，蕴含 evidence | 全部通过 → `environment_certified{env, commit, rule_version, by, validations}`；任一失败 → deny，失败输出可取 |
| `revoke_environment` | `env`, `reason` | 证书有效 | `environment_revoked` |

- 谁能签发，由通信策略决定（例：`{act: certify_environment, from: "role:engineer"}`）。
- 证书记录签发时探针列表与命令的指纹。它在以下情况失效：`revoke_environment`；现行规则中该证书的探针列表或任一探针命令与指纹不符（修宪改了探针）。失效是折叠时计算的，不另写事件，重放同样成立。
- 新的需求条件 `{certificate: <env>}`（requirements 段）：该证书当前有效。规则写法：

```yaml
- name: work-needs-environment        # 创世规则：任何任务的 start 都需要证书
  act: task_transition
  transition: start
  require: [{certificate: workspace}]
```

需要只约束某类任务时加 `where: {task_label: experiment}`。**无证书不可执行任务**（O3 单测）：证书未签发或已失效时 `start` 被拒，理由指出缺哪张证书。

### 5.3 边界

工程岗在本层不能改镜像或安装依赖（沙箱无网络，镜像属于运营者）。探针失败时，工程岗向所有者报告缺口；所有者在两次运行之间补基础设施，工程岗重新签发。环境自建（受权构建镜像）属于第五层的配置演化。

## 6. 目标与停机（O4）

- 目标 = 带 manifest `goals.label` 标签的根任务。创世规则里有两条**受保护**规则（宪法 `protected_rules`，运行中不可删改）：

```yaml
- name: goal-has-predicate
  act: task_create
  where: {task_label: goal}
  require: [{field: acceptance}]
- name: goal-validated
  act: task_transition
  transition: "submit|approve"
  where: {task_label: goal}
  require: [{validate: [acceptance]}]
```

  加上受保护的事实规则 `accepted`（要求 `{validated: acceptance}`），**条件未满足不能关闭目标**（O4 单测）：没有通过的验收，目标任务到不了 done，也就不会有 `accepted` 事实。
- 目标由谁建、是否需要人类批准，由规则决定。创世规则默认 `approvals: {act: task_create, where: {task_label: goal}, approver: "kind:human"}`：所有者确认成功谓词后目标才存在。成功谓词应写明基准与来源（例如「比论文 X 表 3 的 Y 提高 10%，基准数值文件在 …」），这一点由立法者在规则和审计中约束，不由引擎判断语义。
- 停机：
  - 存在至少一个目标，且全部未取消的目标都具有 `goals.fact` → 本层写 `goal_met{goals}`，调度器写 `run_stopped{reason: goal_met}`，所有者收到通知；
  - 所有目标都被取消 → `run_stopped{reason: goals_cancelled}`；
  - 否则按第二层规则（静止、断路器、人工停止）。
- 目标被 `reopen` 时事实撤销，运行可 `resume` 继续。

## 7. 运行时接入

| 注入点 | 本层提供 |
|---|---|
| 规则集加载 | manifest 作为规则集文件，schema 校验（席位名唯一、`count ≥ 1`、`reports_to` 指向存在的席位、`goals` 字段完整）；`environments` 段与 `{certificate}` 条件 |
| `kinds` | `hire`、`dismiss` 的 planner（证书的 planner 在第三层）。这些 kind 登记在 `CommService.gated_kinds`：planner 之后仍经 `InstitutionGate`，规则可以对它们提要求、要审批、要验证 |
| `extensions.attention` | 空缺（给 `staffing`）、受雇（给新成员）、`goal_met`/`goal_reopened`（给人类） |
| `extensions.step` | 按 manifest 同步成员的分组、团队与上级；写 `org_enacted`；按现行策略补齐房间成员；目标判定 |
| `extensions.stop_reason` | `goal_met` / `goals_cancelled`：调度器不再启动新激活，等进行中的激活结束后写 `run_stopped{reason}` |
| `extensions.briefing` | 使命、我的席位与章程、组织表（席位、在职者、空缺）、有效证书、目标及其状态 |
| 调度器 | 新成员在 `member_registered` 后即可被激活；`member_removed` 后不再激活、丢弃其注意项 |

组合根 `sciteam/org/team.py` 的 `OrgTeamRun` 继承 `InstitutionTeamRun`；`open_run` 按团队文件是否有 `org` 选择。第三层代码不 import 本层。

## 8. 组织模板（O5）

`configs/org/templates/<mode>/` 各含 `README.md`（一句话摘要）、`manifest.yaml`、`comm.yaml`；规则沿用创世规则，需要时由立法者修宪。五种模式：

| 模式 | 席位与分组（示意） | 通信策略 |
|---|---|---|
| hierarchical | founder、hr、engineer、lead、developer×n、reviewer | 只能联系上级与直接下属；上级派活 |
| flat | founder、hr、engineer、member×n、reviewer | 全员可互相私聊与 mention；自行领取 |
| workflow | founder、hr、engineer、按阶段的席位 | 任务按依赖流转，同级不私聊 |
| wave | founder、hr、engineer、coordinator、worker×n | 协调者分批派发，一波完成再下一波 |
| market | founder、hr、engineer、worker×n | 任务公开挂出，认领制 |

- 模板只是供创始人参考的文件：`org_templates` 工具列出并显示内容，创始人照抄或改写后按 §3 修宪。代码里没有模式枚举。
- O5 的判定：同一代码、同一创世包，只换创始人采纳的模板，五种组织都能生效并运行（单测：五套模板分别作为修正案生效，席位、策略允许矩阵符合模板）；五模式对比实验属于第五层。

## 9. 工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `org` | 读 | 使命、席位表（在职者、空缺）、分组、有效证书、目标状态 |
| `profiles` / `profile_show` | 读 | 模板目录与全文 |
| `org_templates` | 读 | 组织模板列表与内容 |
| `hire` / `dismiss` | Act | §4 |
| `certify_environment` / `revoke_environment` | Act | §5（第三层工具，组织 harness 启用） |

## 10. 事件

| 事件 | 流 | 载荷 |
|---|---|---|
| `org_enacted` | `org` | `rule_version, seats{name: {count, filled}}, vacancies` |
| `member_registered`（扩展） | `roster` | 原有字段 + `hired_by, seat, template, template_sha, profile_sha` |
| `member_removed`（扩展） | `roster` | `member, by, reason` |
| `environment_certified` / `environment_revoked` | `institution` | `env, commit, by, fingerprint, rule_version, validations` |
| `goal_met` / `goal_reopened` | `org` | `goals, fact` |

所有事件都由 Act 或规则版本变化触发，带 `causation_id`；`check.py` 的重放审计扩展到本层 Act 与事件。

## 11. 测试

| 需求 | 单测 |
|---|---|
| O1 | 未批准的 manifest 修正案：雇佣新席位被拒；批准后同一雇佣被允许；`org_enacted` 空缺正确 |
| O2 | 超员、非法 harness、未知模板被拒；改写档案的 diff 与 sha 入账；新成员被激活并收到简报；解雇后不再激活、任务退回；无匹配模板时 staffing 任务流程走通（脚本化 LLM） |
| O3 | 无证书 `start` 实验任务被拒；探针失败不签发；修宪改探针后证书失效 |
| O4 | 目标无 acceptance 不能创建；验收失败不能 done；全部目标 accepted 后 `run_stopped{goal_met}`；受保护规则不可删 |
| O5 | 五套模板加载并生效，策略矩阵符合模板描述 |
| 通用 | `group:` 选择器；manifest schema；第三层以下不 import `org`（AST 检查）；重放审计一致 |

## 12. 真实验收

同一创世包（使命：实现 `workspaces/minipkg` 的 SPEC，外部隐藏测试打分），flash 模型，所有者由人类担任（批准修正案与目标，其余时间不介入）：

1. 创始人与所有者澄清目标 → 起草并提交 manifest、策略、规则 → 所有者批准（O1）。
2. staffing 组按席位雇佣，至少一次改写模板（O2）。
3. 工程岗编写探针、签发证书；实验/实现任务在证书后才开始（O3）。
4. 目标以隐藏测试之外的机械谓词定义并经所有者批准；运行以 `goal_met` 停止（O4）。
5. 重放审计 0 不一致；另记录运行中是否出现自发修宪（第三层遗留问题）。

另做两次不同模式（创始人被要求采用不同模板）的运行，作为 O5 的真实证据；五模式完整对比在第五层。
