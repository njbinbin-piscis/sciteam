# 40 · 制度（Institution）

> 所属层：3。依赖 `kernel`、`providers`（沙箱）、`comm`、`runtime`。被第四层 `org` 使用。
> 状态：v0.1（2026-09-26）。

## 1. 原则

1. **一个闸门。** 第二层的 `CommService.submit` 是唯一执法点。本层不另开通道：制度规则以 `extra_gate` 接入同一路径，制度自己的 Act（提修正案、投票、审批）也走同一个 `submit`，产生的事件同样带 `causation_id`。
2. **通信即制度。** 通信策略文件（`comm/*.yaml`）是制度文件之一，与本层规则文件一起构成**规则集**，一起计算规则版本，也一起按修宪程序修改。
3. **事实由规则产生，不由模型判定。** 「任务的产物已被接受」这类制度事实，只能由算数规则（counts-as）根据事件和机械验证结果推出。评审人的 `approve` 是一个 Act；它**算作**「接受」，还是只算作一次意见，由规则决定。
4. **验证器是机械的。** 验证器 = 在干净环境里对指定提交运行命令，看退出码。验证器从不调用模型。
5. **宪法很小，其余由委员会立法。** 宪法只写：底层原则（给所有成员看的文字）、哪些文件可修改、哪些规则受保护、修宪程序。其余规则可以一开始就很少，由有权的席位按程序补充。宪法在运行中不可修改，只能由人类在两次运行之间修改。
6. **安全由环境负责，不由制度负责。** 沙箱无网络、工作区隔离、预算断路器属于环境（`31` §6–7）。制度管的是组织内部的权利、义务、审计和奖惩，相当于公司制度，而不是法律。
7. **不加新的语义枚举。** 条件词汇（§5）是一个封闭的小集合，是引擎的「指令集」。规则里写什么由制度文件决定；要增加新的条件种类，就需要改代码并补测试。

## 2. 规则集与版本

一次运行的规则集由三类文件组成：

| 文件 | 内容 | 运行中可改 |
|---|---|---|
| `institution/<name>.yaml`（宪法） | 原则、规则文件引用、修宪程序、受保护规则 | 否 |
| `institution/<name>-rules.yaml`（规则） | 要求、审批、验证器、算数规则、账本 | 按宪法 |
| `comm/<policy>.yaml`（通信策略） | 30 §7 | 按宪法 |

- 团队文件新增 `institution: institution/<name>.yaml`。不写这一项时，制度层为空：只有通信策略，行为与第二层完全相同。
- **规则版本** = `sha256` of 规则集中各文件 `(path, sha256(text))` 按 path 排序后的拼接，取前 12 位。运行开始时写 `rule_version_activated{version, files}`，每次修正案生效时再写一次。
- 每条 `act_submitted` 与 `act_decided` 的 `rule_version` 列记录裁决时的版本。修正案生效之前提交的 Act 按旧版本裁决，旧事件按旧规则解释（I3）。
- `rule_version_activated{version, files, texts, amendment}` 直接带上规则集全部文件的文本（几 KB），所以任何时刻的规则集都能只凭事件库重建，重放审计不依赖文件系统。

## 3. 宪法（`institution/<name>.yaml`）

```yaml
name: audited
principles: |                     # 进入每个成员的简报；写原则，不写流程
  Every deliverable is audited before it counts. ...
rules: institution/audited-rules.yaml
amendment:
  amendable: [institution/audited-rules.yaml, comm/flat.yaml]
  protected_rules: [submit-validated, four-eyes]   # 这些名字的规则必须存在且内容不变
  proposers: "role:lead | role:reviewer"          # 30 §7.2 选择器语言
  voters: "role:lead | role:reviewer"
  pass: {yes_at_least: 2, no_at_most: 0}          # 票数条件；`yes_at_least: all` = 全体投票人
  human_ratify: false                             # true：投票通过后还需人类批准
  voting_window_s: 3600                           # 超时未通过即失效
```

没有 `amendment` 段的宪法不允许修正案。

## 4. 规则文件（`institution/<name>-rules.yaml`）

```yaml
validators:
  tests:                                    # 名字供规则引用
    command: "python -m pytest -q"
    timeout_s: 900
  acceptance:
    command: "{task.acceptance}"            # 模板：任务自带的验收命令
    timeout_s: 900

requirements:                               # 对 Act 的附加条件；按顺序全部检查
  - name: tasks-have-acceptance
    act: task_create
    require: [{field: acceptance}]
  - name: submit-validated
    act: task_transition
    transition: submit
    require: [{evidence: commit}, {validate: [tests, acceptance]}]
  - name: four-eyes
    act: task_transition
    transition: approve
    require: [{distinct: [actor, task.assignee]}]

approvals:                                  # 需要某人批准后才生效的 Act
  - name: release-signoff
    act: task_transition
    transition: approve
    where: {task_label: release}
    approver: "kind:human"

facts:                                      # counts-as：什么「算作」什么
  - name: accepted
    subject: task
    when: {event: task_status_changed, to: done}
    require: [{validated: tests}, {approved_by: "!task.assignee"}]
    revoke_when: {event: task_status_changed, transition: reopen}

ledger:                                     # 奖惩记账；只记账，不自动执行后果
  - {name: delivered, when: {fact: accepted}, to: task.assignee, amount: 10}
  - {name: reviewed,  when: {fact: accepted}, to: task.reviewer, amount: 3}
  - {name: red-build, when: {validation: failed}, to: act.actor, amount: -2}
  - {name: sent-back, when: {transition: reject}, to: task.assignee, amount: -3}
```

`environments: {<name>: {probes: [<validator>, ...]}}`（第六段）定义环境证书：`certify_environment{env, commit}` 对 commit 运行这些探针验证器，全部通过则写 `environment_certified`（50 §5）。它与 `hire`、`dismiss` 一样登记在 `CommService.gated_kinds`：planner 做结构与通信策略检查后仍经 Gate。

宪法 `human_ratify` 可以是 `true`，也可以是路径列表，只对这些文件的修正案要求人类批准。

所有段都可以省略；空文件就是空制度。

## 5. 条件词汇（封闭集合）

`require` 里的每一项是下列之一。`where` 只能用 `task_label`、`task_is_root`。

| 条件 | 用在 | 含义 |
|---|---|---|
| `{field: <name>}` | requirements | Act 创建或更新的任务，该字段非空（`acceptance`、`reviewer`、`estimate_h`、`labels`） |
| `{evidence: commit}` | requirements | Act 带 `params.commit`，且能在工作区仓库里解析为提交；解析出的完整 sha 记在 `validation_requested.commit` |
| `{validate: [v, ...]}` | requirements | 对证据提交运行这些验证器，全部通过才生效；运行期间 Act 处于 pending。蕴含 `evidence`：Act 未给出可解析的 `commit` 时直接拒绝，不发验证请求 |
| `{distinct: [a, b]}` | requirements | 两个角色引用（`actor`、`task.assignee`、`task.reviewer`、`task.creator`）指向不同成员 |
| `{validated: v}` | facts | 该任务最近一次 submit 的证据提交上，验证器 v 通过 |
| `{approved_by: <selector>}` | facts | 使任务进入 done 的 `approve`（或无评审人时的 `submit`）发起者满足选择器 |
| `{children_have: <fact>}` | facts | 该任务的全部未取消子任务都具有该事实 |
| `{certificate: <env>}` | requirements | 该环境证书现行有效（50 §5）：已由 `certify_environment` 签发，且签发时的探针指纹与现行规则一致 |
| `{progress: {validator: v, identical_failures: k}}` | requirements | 无进展停止：该任务上验证器 v 最近 k 次已完成的运行全部失败，且失败输出末尾（`tail`）完全相同时，拒绝该 Act。拒绝理由给出规则名、失败次数和重复的输出，并提示取消任务或升级给所有者。只看同一任务、同一验证器；有一次通过或输出不同即重新计数。用途：失败信号不含可行动信息时，阻止「返工、同样失败」的死循环耗尽预算（60 D11） |

`task.acceptance` 是任务的新字段（30 §5 同步）：可在创建时给出，也可用 `task_update` 修改。验收命令由任务创建者写，不由负责人写。

条件在哪一段可用由 schema 检查（requirements：`field`、`evidence`、`validate`、`distinct`、`children_have`、`certificate`、`progress`；facts：`field`、`distinct`、`validated`、`approved_by`、`children_have`），放错段的规则文件无法加载。

## 6. 裁决流程

```
Act ─► CommService.submit
        ├─ act_submitted{rule_version}
        ├─ 结构检查 + 通信策略（第二层）
        ├─ InstitutionGate.decide(act, view)
        │    ├─ requirements：不满足 → deny（rule = 规则名）
        │    ├─ 需要验证 → pending{waiting: [validation ids]}
        │    └─ 命中 approvals → pending{waiting: [approval]}
        ├─ act_decided{decision, pending_id?, waiting?}
        └─ allow → 领域事件（causation = act_decided）
```

### 6.1 Pending

- `pending` 时不写领域事件，只写 `act_decided{decision: pending, waiting}`。工具返回 `pending: waiting for <...>; you will be notified`，并附上验证 id 或审批人。
- 需要验证时，同时写 `validation_requested{validation_id, validator, commit, command, act_id}`，由运行时的验证工作器执行（§7）。
- 等待的事项全部完成后，由 InstitutionService 写 `act_resolved{act_id, decision, reason}`。
  - 解决时按**当前**状态重新执行结构检查、通信策略和制度要求（已完成的验证与审批视为满足）。世界在等待期间变了，比如任务被别人取消，则 deny。
  - allow 时领域事件的 causation 指向 `act_resolved`。
- 验证失败 → deny，理由里给出验证器名、退出码和输出尾部（完整输出在 blob 中，`read_output` 可取）。
- 发起者收到注意项 `act_resolved`（总是唤醒）。同一任务同一时刻只能有一个 pending 的迁移：会进入 pending 的第二个迁移直接 deny（rule `pending`）；不需要等待的迁移（如创建者 cancel）照常裁决，等待中的 Act 解决时按新状态重新裁决。
- 解决时机：等待项全部完成、任一项失败（验证失败、审批被拒）、或任务已进入终态时，由 `settle()` 调用 `CommService.resolve`。

### 6.2 人类审批

- 命中 `approvals` → 写 `approval_requested{key, approval, approver, actor, summary}`（key = act id；修正案批准时为修正案 id）。审批 Act 写 `approval_decided{key, decision, by, note}`。满足选择器的审批人获得注意项 `approval_requested`：agent 被唤醒；人类在收件箱和 `sciteam team status` 里看到。
- 审批 Act：`decide_pending{act_id, decision: approve|reject, note}`，发起者必须满足该审批的选择器。reject 必须写 note。
- 未批准的 Act 不产生任何领域事件（I4）。

## 7. 验证器执行

- 验证工作器（`institution/validation.py`）随调度器运行，会阻止系统静止。它读取 `validation_requested`，按 `(validator, commit, 渲染后的命令)` 查缓存，命中直接复用结果。
- 执行方式：
  1. 把证据提交导出到临时目录（`git -C workspace archive <sha> | tar -x`），与工作区隔离，所以 agent 未提交的改动不影响结果。
  2. 起一个与团队相同镜像、无网络的容器，挂载该目录为 `/workspace`，运行命令，超时即失败。
  3. 写 `validation_finished{validation_id, ok, exit_code, wall_s, output_blob, tail, cached_from, infra}`；完整输出在 BlobStore，`validation_output(id)` 工具可分页读取。
- 基础设施故障（导出失败、容器起不来）不算提交者的失败：重试 3 次，仍失败则记 `infra: true`，账本不扣分。
- 验证器命令在规则文件里，或由任务创建者写在 `acceptance` 里；负责人不能改自己任务的验收命令（`task_update` 的 `acceptance` 字段需要发起者是任务创建者）。

## 8. 制度事实与账本

- 制度折叠（`InstitutionView`）自带一份 CommView，按全局顺序与制度事件一起折叠，所以每条推导都在**触发事件那一刻**的状态上求值，与写入时机无关（重放也一样）。折叠时按 `facts` 规则对每个匹配 `when` 的事件求值 `require`：成立则记为应写（owed）`fact_established{fact, subject, rule, by, basis}`；匹配 `revoke_when` 则记 `fact_revoked`。`settle()` 把应写项按触发顺序写入，二者的 causation 都指向触发事件；写入后折叠把它从应写项中移除，所以推导幂等、重启后不重复。
- `ledger` 规则按 `when`（不用 `on`：YAML 1.1 把它读成布尔值）匹配事实、验证结果或迁移，写 `ledger_entry{member, amount, rule, task, basis}`。
- 账本在本层只记账，只进入简报（「你的记录」一节）和 `standing` 工具。它对雇佣或权限产生什么后果，由第四层的制度决定。
- 事实和账本都是规则对事件的确定性解释。重放审计会用当时的规则版本重新推导，再与记录的事件比对。

## 9. 修宪

| Act | 条件 | 事件 |
|---|---|---|
| `propose_amendment{path, content, rationale}` | 发起者满足 `proposers`；path 在 `amendable` 中；新规则集整体能加载；受保护规则仍存在且内容不变（含它引用的验证器定义，防止改验证器命令把规则掏空） | `amendment_proposed{amendment_id: A<n>, path, content, base_sha, new_sha, proposer, rationale, votes}`；工具返回 diff |
| `vote{amendment, choice: yes\|no, note}` | 投票人满足 `voters`；修正案仍在表决期；每人一票，可改票 | `amendment_voted` |
| `withdraw_amendment{amendment}` | 发起者本人 | `amendment_closed{reason: withdrawn}` |
| `decide_pending`（`human_ratify`） | 人类 | 同 §6.2 |

- 发起者若是投票人，提出即计一票赞成。
- 票数满足 `pass` 后：`human_ratify` 时写 `amendment_passed` 和 `approval_requested{key: A<n>}`，人类 `decide_pending` 批准后生效、拒绝则 `amendment_closed{reason: not_ratified}`；否则直接生效：写 `amendment_enacted{amendment_id, path, new_sha, rule_version, proposer}` 和 `rule_version_activated`。之后的 Act 按新版本裁决；调度器使用的 `wake_on`、`limits` 同步更新。
- 生效时，如果目标文件的当前 sha 已不是 `base_sha`（另一修正案先生效），写 `amendment_closed{reason: conflict}`，需要重新提出。
- 同一次 settle 可以有多个修正案生效（改的是不同文件）。每个修正案都替换**现行版本**中的那一个文件，不能基于本轮开始时的快照，否则后生效的会悄悄撤销先生效的（genesis1 实测：A3 撤销了 A2 的通信策略，重放审计发现 66 处裁决不一致；回归测试 `test_amendments_passing_together_all_take_effect`）。
- 超过 `voting_window_s` 写 `amendment_closed{reason: expired}`。
- 投票人获得注意项 `amendment_vote`，发起者获得 `amendment_closed` / `amendment_enacted`。
- 修正案提交的是整个文件的新内容，工具返回与现行版本的 diff。

## 10. 运行时接入

第二层保持对第三层无感知：制度层通过以下注入点接入，不修改第二层的语义。

| 注入点 | 位置 | 制度层提供 |
|---|---|---|
| `extra_gate` | `CommService` | `InstitutionGate` |
| `kinds` | `CommService` | 制度 Act 的 planner（`propose_amendment`、`vote`、`withdraw_amendment`、`decide_pending`） |
| `extra_tools` | `AgentRunner` | §11 工具 |
| `extensions`（`SchedulerExtension`） | `Scheduler` | `attention(ev, view)`：制度事件的注意项（带 `note`，总是唤醒）；`step()`：每轮调用 `settle()` 与验证工作器；`busy()`：验证进行中时不静止；`briefing(member)`：原则、我的记录、待我处理的制度事务 |
| `set_policy` | `CommService`、`Scheduler` | 通信策略修正案生效后替换策略 |

组合根 `sciteam/institution/team.py` 的 `InstitutionTeamRun` 继承 `runtime.team.TeamRun`，覆写其钩子（`_make_comm`、`on_create`、`extensions`、`extra_tools`、`tool_extras`、`status_lines`）；`open_run` 按团队文件是否有 `institution` 选择。CLI 只通过它创建和驱动运行；`sciteam team decide RUN --as <human> <id> approve|reject [--note]` 记录人类审批。

只等人类审批的 pending 不阻止静止：批处理运行会停下，操作者 `decide` 后 `resume` 继续。

## 11. 工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `task_transition` | Act | 新增参数 `commit`（submit 的证据） |
| `task_create` / `task_update` | Act | 新增字段 `acceptance` |
| `rules` | 读 | 当前规则版本、宪法原则、适用于自己的要求、可修改文件及其当前内容 |
| `propose_amendment` / `vote` / `withdraw_amendment` | Act | §9 |
| `decide_pending` | Act | §6.2 |
| `standing` | 读 | 账本（自己的明细与全员合计）、制度事实 |
| `pending` | 读 | 自己发起的、等自己审批的 pending Act；进行中的修正案 |
| `validation_output` | 读 | 一次验证的完整输出（分页） |

## 12. 事件

| 流 | 事件 |
|---|---|
| `institution` | `rule_version_activated`、`validation_requested`、`validation_finished`、`approval_requested`、`approval_decided`、`fact_established`、`fact_revoked`、`ledger_entry` |
| `gate` | `act_resolved`（与 `act_submitted`、`act_decided` 同流） |
| `amendment/<A_n>` | `amendment_proposed`、`amendment_voted`、`amendment_passed`、`amendment_enacted`、`amendment_closed` |

## 13. 模块

| 文件 | 职责 |
|---|---|
| `institution/model.py` | 宪法、规则文件 schema；条件词汇；规则集与版本 |
| `institution/view.py` | 制度折叠：规则集历史、pending、验证、事实、账本、修正案 |
| `institution/gate.py` | `InstitutionGate`：requirements、approvals、pending 判定 |
| `institution/facts.py` | counts-as 与账本求值（纯函数） |
| `institution/service.py` | 制度 Act 的 planner、pending 解决、事实与账本写入、修正案生效 |
| `institution/validation.py` | 验证工作器（导出提交、容器执行、缓存） |
| `institution/tools.py` | §11 工具 |
| `institution/team.py` | 组合根：规则集加载、注入第二层 |

## 14. 测试

单元测试（纯函数，无 IO）：

- 规则集版本：同内容同版本，任一文件改动版本变化。
- 条件词汇逐项测试；where 过滤。
- requirements：缺证据 → deny；四眼原则 → deny；满足 → allow。
- counts-as：验证失败时不产生事实（I2）；reopen 撤销事实；`children_have` 递归。
- 账本规则匹配。
- 修正案：不可修改路径、schema 错误、改动受保护规则 → deny；票数与改票；冲突；过期。

场景测试（fake LLM，`LocalSandbox` 验证器）：

- I1：源码检查（AST），除 `comm/service.py` 与 `institution/service.py` 外没有模块写领域事件和制度事件类型；`InstitutionGate.on_pending` 只返回事件，由 CommService 写入；工具只通过 `CommClient.submit` 产生作用。
- submit → pending → 验证通过 → allow；验证失败 → deny + 账本扣分；等待期间任务被取消 → 解决时 deny。
- 人类审批：未批准时无领域事件；批准后生效；拒绝后 deny。
- 修正案通过后新 Act 按新版本裁决、旧 Act 的记录不变；通信策略修正案改变可达性和 `wake_on`。
- 重放：跨规则版本重放全部 Act，决定、领域事件、事实、账本一致。
- 崩溃恢复：验证进行中进程退出，重启后未完成的验证重跑，pending 照常解决。

## 15. 真实模型验收（I1–I6）

1. **审计制度运行**（`teams/minipkg-audited.yaml`）：flat 通信 + `institution/audited`，模型 flash，之后 pro。
   - 规则：任务必须带验收命令，submit 必须带提交且验证通过，四眼原则，发布任务需要人类签字，账本如 §4。
   - 判定（`bench/team/check.py` 扩展）：
     - 每个 done 任务都有通过的验证记录；
     - 出现过验证失败被拒并扣分的记录；
     - `accepted` 事实与 done 任务一一对应（依规则）；
     - 发布在人类批准前停留在 in_review；
     - 重放一致；
     - 隐藏测试得分。
   - 人类签字由操作者用 `sciteam team decide` 完成，操作记录进事件库。
2. **修宪运行**（`teams/minipkg-legislate.yaml`）：宪法只给原则和程序，规则文件几乎为空，但带一条明显妨碍工作的通信限制；委员会为 lead 与 reviewer。目标文本告知团队规则可以按程序修改。
   - 判定：出现修正案提出、表决、生效，并有新规则版本；之后的裁决引用新版本；试图改动受保护规则被拒（若出现）；跨版本重放一致。
   - 团队没有自发修宪时，加一个定向探针：人类要求委员会修改某条规则。探针结果单独报告，不与自发修宪混在一起。
