# 30 · 通信：地址、会话、消息、任务、通信策略

> 所属层：2。依赖 `kernel`、`harness`（工具协议）。被 `runtime`（调度器）与第三层 `institution` 使用。
> 目标：agent 之间像人一样协作——私聊、在聊天室里 @ 人、在线程里讨论、在看板上派活和认领；**哪些人能和哪些人说话、谁能派活、谁能认领，全部由通信策略文件决定**，内核不写任何拓扑。

## 1. 设计原则

1. **一切皆事件**。会话、消息、任务、成员关系都是事件流的折叠；没有可变表。
2. **每个对外产生效力的通信动作都是一次 Act**，经 Gate 裁决后才产生领域事件。第二层的 Gate 实现是 `PolicyGate`：按通信策略裁决可达性、权限和速率。第三层用规则引擎扩展同一个 Gate 接口，不改通信代码。**通信策略就是第一份制度。**
3. **任务的讨论就是一个会话**。每个任务自带一个会话 `task:<id>`，评论、提交说明、评审意见都是这个会话里的消息，不再有第二套评论模型。
4. **拓扑是配置**。层级、扁平、工作流、波次、认领市场五种协作模式只用策略文件和任务依赖表达（§7），代码里没有 `if mode == ...`。
5. agent 能看到规则：简报里列出「你可以联系谁、可以做什么」，被拒绝的 Act 以工具错误返回并入账。

## 2. 参与者与名册

参与者（principal）有三种：agent、人类、系统。

```python
class Member(BaseModel):
    id: str                  # 地址名：小写字母、数字、-、_；全局唯一
    kind: Literal["agent", "human"]
    role: str = ""           # 自由文本，策略选择器可按它匹配
    team: str = ""
    manager: str | None = None
    harness: str | None = None   # agent 用哪份 harness 配置（configs/harness/*.yaml）
    profile: str = ""        # agent 的角色说明，进入系统提示
    attrs: dict[str, str] = {}
```

名册是 `roster` 流的折叠：`member_registered`、`member_updated`、`member_removed`。第二层在运行开始时从团队文件（§8）写入这些事件；第四层的雇佣与离职写同样的事件，所以名册天然支持组织在运行中变化。

## 3. 地址

| 写法 | 含义 |
|---|---|
| `@<id>` | 某个成员；作为 `send_message` 的 `to` 表示私聊，出现在正文里表示 mention |
| `@human` | 名册中所有 kind=human 的成员（运行时的「上报人类」出口） |
| `#<room>` | 聊天室 |
| `task:<id>` | 某任务的讨论会话 |

正文中的 mention 用正则 `(?<![\w@])@([a-z0-9][a-z0-9_-]*)` 提取，只保留名册中存在的 id（加上 `human`）。`send_message` 另有 `mentions` 参数，与正文提取结果取并集，方便模型显式 @ 人。

## 4. 会话与消息

### 4.1 会话

```python
class Conversation(BaseModel):
    id: str                          # "dm:<a>+<b>"（a<b 排序）| "room:<name>" | "task:<id>"
    kind: Literal["dm", "room", "task"]
    members: set[str]
    topic: str = ""
    created_by: str
    closed: bool = False
```

- 私聊会话在第一次发消息时隐式创建（`conversation_created` 与 `message_posted` 由同一个 Act 产生）。
- 房间由 `create_room` 创建，或在团队文件、策略 `rooms` 里预置；成员由 `add_member`/`remove_member` 变更（受策略约束）。
- 在房间或任务会话里 mention 非成员：被 mention 者自动加入该会话（相当于抄送），否则他被唤醒却读不到内容。在私聊里 mention 第三方被结构性拒绝（第三方读不到私聊），提示改为直接私聊他。
- 在任务会话里发言的非相关人自动成为该会话成员。
- 任务会话在 `task_created` 时一起创建，成员是创建者、负责人、评审人与关注者，随任务字段变化自动更新。
- **可见性**：只有会话成员能读写。`read_conversation` 对非成员返回错误。人类成员的可见性由策略 `humans_see_all`（默认 true）决定——这是观察者权限，不是 agent 权限。

### 4.2 消息

```python
class Message(BaseModel):
    msg_id: str                  # m<n>：运行内全局序号，短，便于模型引用与回复
    conv: str
    author: str
    text: str                    # 上限 policy.limits.message_chars，超出拒绝（不截断）
    mentions: list[str]
    reply_to: str | None         # 被回复的 msg_id
    thread: str                  # 线程根 msg_id；非回复消息的 thread = 自身 msg_id
    attachments: list[Attachment]  # 工作区路径或 blob 引用，不内联大内容
```

- 线程：`reply_to` 指向同会话内的消息，`thread` 取被回复消息的 `thread`，所以线程是扁平的（Slack 式），不无限嵌套。
- 消息写入 `conv/<id>` 流，事件类型 `message_posted`。消息不可编辑、不可删除；更正就是再发一条。

## 5. 任务与看板

### 5.1 数据模型

```python
class Dependency(BaseModel):
    task: str                                   # 前置任务 id
    type: Literal["FS", "SS", "FF", "SF"] = "FS"

class Task(BaseModel):
    id: str                                     # T<n>：运行内序号，兼作显示名
    title: str
    description: str
    parent: str | None                          # WBS 父任务
    deps: list[Dependency]
    status: TaskStatus
    created_by: str
    assignee: str | None
    reviewer: str | None                        # 为空则提交即完成
    watchers: set[str]
    estimate_h: float | None                    # 供关键路径计算
    labels: list[str]
    acceptance: str | None                      # 验收命令；只有创建者可改（制度层验证器可引用，40 §4）
    artifacts: list[Attachment]                 # 提交的产物
    started_at / finished_at: datetime | None
```

任务写入 `task/<id>` 流。看板是全部任务流的折叠，列即状态。

### 5.2 状态机

```
            assign / claim              start               submit
  open ──────────────────▶ assigned ─────────▶ in_progress ─────────▶ in_review
   ▲  ◀──── unassign ────                         │   ▲                  │  │
   │                                              │   │ rework (reject)  │  │ approve
   │                        block / unblock ◀─────┘   └──────────────────┘  ▼
   │                          (blocked)                                    done
   └──────────────────────────── reopen ◀──────────────────────────────────┘
  任意非终态 ── cancel ──▶ cancelled
```

| 迁移 | 前置状态 | 默认可执行者 | 附加条件 |
|---|---|---|---|
| `assign(to)` | open, assigned | 创建者、父任务负责人、策略 `task.assign` 允许者 | 被指派者须在策略允许的派活对象中 |
| `claim` | open | 策略 `task.claim` 允许者 | 乐观并发：同一时刻只有一个认领成功 |
| `unassign` | assigned | 负责人本人、指派者 | — |
| `start` | assigned, blocked(仅解除后) | 负责人 | **启动类依赖满足**（§5.3） |
| `block(reason)` / `unblock` | in_progress / blocked | 负责人 | block 时通知创建者 |
| `submit(artifacts, note)` | in_progress | 负责人 | **完成类依赖满足**；无评审人时直接进入 done |
| `approve(note)` / `reject(note)` | in_review | 评审人 | reject 回到 in_progress |
| `reopen(reason)` | done | 创建者、评审人 | 下游已开始的任务收到通知 |
| `cancel(reason)` | 非终态 | 创建者 | 子任务一并取消 |

- `reopen` 回到 `assigned`（有负责人时）或 `open`；负责人需重新 `start`，此时再次检查启动类依赖。
- 「状态机合法性」是结构约束，由 `TaskBoard.check(act)` 判定；「谁能做」由策略判定。两者都在 Gate 里执行，任一失败都记 `act_decided{decision: deny}`。
- 第二层的 `approve` 只是评审人的判断；第三层会加「counts-as」规则（例如必须附带通过的测试证据），同一迁移届时才产生制度事实。

### 5.3 依赖类型（PMBOK 定义）

对依赖 `B.deps ∋ {task: A, type}`：

| 类型 | 约束 B 的哪个迁移 | 满足条件 |
|---|---|---|
| FS | `start` | A 为 done |
| SS | `start` | A 已开始（in_progress 及之后，或 done） |
| FF | `submit` | A 为 done |
| SF | `submit` | A 已开始 |

- 添加依赖时检测环（DFS），成环则拒绝。
- A 被 cancel：依赖视为「已解除」并通知 B 的负责人与创建者，由人判断，不自动级联。
- A 被 reopen：已满足的 FS/FF 依赖回到未满足；B 若已开始不回滚，只通知。
- 依赖变为满足时产生注意项 `task_ready` 给 B 的负责人（`start` 或 `submit` 被解锁）。

### 5.4 关键路径与看板视图

`task_board` 工具返回看板视图：各列任务、每个任务的就绪情况（哪些依赖未满足）、负责人、阻塞原因。有 `estimate_h` 时给出关键路径（CPM：前推最早开始/完成，后推最晚开始/完成，总浮动为 0 的链），SS/FF/SF 按标准 CPM 公式处理，缺估计的任务按 0 计并在视图中标注。有子任务的 WBS 汇总任务不参与排程（由其子任务体现）。这让管理者 agent 能按项目管理方式排程，而不是由调度器替它排。

## 6. Act 与 Gate

### 6.1 契约

```python
class Act(BaseModel):
    act_id: str
    actor: str                     # 发起者成员 id
    kind: str                      # 见 6.2
    params: dict[str, Any]
    activation_id: str | None      # 发起时所在的激活
    rule_version: str | None = None

class Decision(BaseModel):
    decision: Literal["allow", "deny", "pending"]
    reason: str = ""
    rule: str | None = None        # 命中的策略规则名，便于审计

class Gate(Protocol):
    def decide(self, act: Act, view: CommView) -> Decision: ...
```

`CommView` 是名册、会话、看板、速率计数的只读折叠。

- 执行路径：工具构造 Act → `CommService.submit(act)` → 写 `act_submitted`（`gate` 流）→ `gate.decide` → 写 `act_decided` → 若 allow，按 Act 类型写领域事件（`causation_id` = `act_decided` 的 seq）→ 返回工具结果。整个过程持有 CommService 的写锁，决定与生效之间没有竞态。
- `pending` 是第三层（人类审批）的出口；第二层 `PolicyGate` 不产生 pending。
- 被拒绝的 Act 以 `error: denied by policy (<rule>): <reason>` 返回给 agent。

### 6.2 Act 类型

| kind | 领域事件 |
|---|---|
| `post_message` | （可能）`conversation_created`、`message_posted` |
| `create_room` / `add_member` / `remove_member` | `conversation_created` / `conv_member_added` / `conv_member_removed` |
| `task_create` | `task_created`、`conversation_created`(task) |
| `task_transition` | `task_status_changed`（`assign`、`claim` 等作为 `transition` 参数） |
| `task_update` | `task_updated`（标题、描述、依赖、评审人、关注者、估计） |

## 7. 通信策略（`configs/comm/*.yaml`）

### 7.1 Schema

```yaml
name: hierarchical
description: 每人只与上级和直接下级私聊；团队房间；只有上级派活
selectors:                     # 可选：命名选择器，供规则引用
  leads: "role:lead"
rules:                         # 按顺序匹配，首个命中的规则决定（effect 默认 allow，可写 deny）；无规则命中 → deny
  - {name: dm-up,     act: post_message, via: dm,   from: "*", to: "self.manager"}
  - {name: dm-down,   act: post_message, via: dm,   from: "*", to: "self.reports"}
  - {name: rooms,     act: post_message, via: "room|task", from: "*"}
  - {name: mention,   act: mention,                from: "*", to: "self.team | self.manager"}
  - {name: human,     act: post_message, via: dm,   from: "*", to: "human"}
  - {name: assign,    act: task_transition, transition: assign, from: "*", to: "self.reports"}
  - {name: create,    act: task_create,   from: "*"}
  - {name: work,      act: task_transition, transition: "start|block|unblock|submit", from: "task.assignee"}
  - {name: review,    act: task_transition, transition: "approve|reject", from: "task.reviewer"}
  - {name: owner,     act: task_transition, transition: "reopen|cancel|unassign", from: "task.creator"}
rooms:                         # 运行开始时预置
  - {name: "team-{team}", for_each: team, members: "team:{team}"}
wake_on: [dm, mention, thread_reply, task_assigned, task_ready, task_review, task_returned, timer]
limits:
  message_chars: 8000
  messages_per_agent_per_min: 12
  messages_per_pair_per_10min: 30
humans_see_all: true
```

### 7.2 选择器语言

有意做成小而封闭的语言（不可图灵完备，便于审计与论文陈述）：

- 原子：`$<名字>`（引用 `selectors` 中的命名选择器）、`*`、`<id>`、`role:<r>`、`team:<t>`、`group:<g>`（成员 `attrs.groups` 含 g；分组由第四层 manifest 维护，50 §2.1）、`kind:human`、`human`、`self`、`self.manager`、`self.reports`、`self.team`、`self.peers`（同 manager）、`room.member`（目标会话成员）、`task.party`（任务相关人）、`task.assignee`、`task.reviewer`、`task.creator`、`task.parent_assignee`。
- 组合：`a | b`（并）、`a & b`（交）、`!a`（补）。
- `from` 匹配发起者；`to` 匹配目标（私聊对象、被 mention 者、被指派者）。一条消息 mention 多人时对每个被 mention 者分别检查，任一不允许则整条消息拒绝，并在理由中列出不允许的对象。

### 7.3 `act` 的判定对象

| act | 目标 |
|---|---|
| `post_message` + `via: dm` | 私聊对象 |
| `post_message` + `via: room/task` | 无目标，只匹配 `from`（可用 `room.member`、`task.party` 约束发起者）。房间的成员资格本身是结构约束：非成员不能在房间发言（人类在 `humans_see_all` 下除外） |
| `mention` | 每个被 mention 者（独立于 `post_message`，两者都要通过） |
| `task_transition` + `assign` | 被指派者 |
| 其他 | 无目标，只匹配 `from` |

### 7.4 五种协作模式的配置（`configs/comm/`）

| 文件 | 要点 |
|---|---|
| `flat.yaml` | 所有私聊、mention、建房间、建任务、认领均允许；`#general` 全员 |
| `hierarchical.yaml` | §7.1；私聊只向上/向下；只有上级派活；无认领 |
| `workflow.yaml` | agent 之间**不能**私聊和 mention；只能在 `task:` 会话里写提交说明；任务与 FS 依赖由团队文件预置（DAG）；唤醒只靠 `task_assigned`/`task_ready`/`task_returned` |
| `wave.yaml` | 协调者可派活与建任务；执行者只能与协调者私聊；波次用依赖表达：第 k+1 波每个任务对第 k 波每个任务有 FS 依赖（或依赖一个里程碑任务），调度器无波次概念 |
| `market.yaml` | 任务 open 发布，`claim` 对所有 agent 允许，`assign` 不允许；有可认领任务时以 `task_open` 唤醒有认领权的人；房间 `#market` 全员可发言 |

「允许通信 / 不允许通信」这一对照就是 `flat` 与 `workflow` 的差别。

## 8. 团队文件（`configs/teams/*.yaml`，第二层的名册来源）

```yaml
name: pkg-team
comm_policy: comm/hierarchical.yaml
members:
  - {id: lead,   kind: agent, role: lead,   team: core, harness: harness/team-default.yaml,
     profile_ref: profiles/lead.md}
  - {id: dev1,   kind: agent, role: worker, team: core, manager: lead, harness: harness/team-default.yaml,
     profile_ref: profiles/developer.md}
  - {id: owner,  kind: human, role: owner}
rooms: []            # 额外预置房间
tasks: []            # 预置任务（workflow 模式的 DAG），字段同 task_create
```

第四层用组织 manifest 与雇佣替代静态团队文件；两者写同样的 `member_registered` 事件。

## 9. 通信工具（agent 可用）

| 工具 | readonly | 说明 |
|---|---|---|
| `send_message(to, text, reply_to?, mentions?, attachments?)` | 否 | `to` 为 `@id`、`#room` 或 `task:<id>` |
| `read_conversation(conv, thread?, before?, limit=30)` | 是 | 按时间倒序分页；返回每条消息的 msg_id 以便回复 |
| `list_conversations()` | 是 | 我所在的会话及未读数 |
| `create_room(name, members, topic)` / `add_member` / `remove_member` | 否 | 受策略约束 |
| `directory()` | 是 | 名册 + **我按当前策略可以私聊/mention/派活的对象** |
| `task_create(title, description, parent?, deps?, assignee?, reviewer?, estimate_h?, labels?, acceptance?)` | 否 | 可同时指派 |
| `task_transition(task, transition, to?, note?, artifacts?, commit?)` | 否 | §5.2 的迁移；`note` 同时作为消息写入任务会话；`commit` 是 submit 的证据提交（记在事件里，制度层可要求） |
| `task_update(task, …)` | 否 | 改依赖时做环检测；改 `acceptance` 需发起者是任务创建者 |
| `task_board(filter?)` | 是 | 看板 + 就绪情况 + 关键路径 |
| `task_show(task)` | 是 | 任务详情、依赖、历史迁移、会话最近消息 |
| `schedule_wakeup(after_s, note)` | 否 | 只影响自己，不是 Act；见 31 §5 |

工具统一实现 harness 的 `Tool` 协议，通过 `ToolContext.extras["comm"]`（一个绑定了 actor 与 activation_id 的 `CommClient`）访问服务，harness 本身不依赖 comm。

## 10. 事件清单

| 流 | 事件 |
|---|---|
| `roster` | `member_registered`、`member_updated`、`member_removed` |
| `gate` | `act_submitted`、`act_decided` |
| `conv/<id>` | `conversation_created`、`conv_member_added`、`conv_member_removed`、`message_posted` |
| `task/<id>` | `task_created`、`task_updated`、`task_status_changed` |

## 11. 模块

| 模块 | 职责 |
|---|---|
| `comm/model.py` | Member、Conversation、Message、Task、Dependency、Act、Decision |
| `comm/view.py` | `CommView`：由事件折叠得到的名册、会话、看板；增量应用 |
| `comm/board.py` | 状态机表、依赖满足判定、环检测、CPM |
| `comm/policy.py` | 策略 schema、选择器解析与求值、`PolicyGate`、速率计数 |
| `comm/service.py` | `CommService.submit(act)`：Gate → 领域事件；`CommClient` |
| `comm/tools.py` | §9 工具 |
| `comm/render.py` | 消息、会话、看板的文本渲染（供工具结果与简报使用） |

## 12. 测试清单

- 单元：
  - 选择器解析与求值（每个原子、`| & !`、未知原子加载时报错）；
  - 策略首条命中与默认拒绝；多 mention 中一个不允许则整条拒绝；
  - 状态机：每条合法迁移成功、每条非法迁移被拒且有 `act_decided{deny}`；
  - 四种依赖类型的满足判定；环检测；reopen 使 FS 回到未满足；
  - 并发认领：两个 actor 同时 claim，恰有一个成功；
  - CPM 在教科书例题上的最早/最晚时间与关键路径；
  - mention 提取（邮箱、代码中的 `@decorator` 不误判：只保留名册中的 id）。
- 可见性：非成员 `read_conversation` 报错（C1、C2）；线程根计算正确。
- 折叠一致性：`CommView` 增量应用与从头折叠结果相同。
- 策略对照（C5）：同一组 Act 序列在五份策略下的允许/拒绝矩阵与期望一致（快照测试）。
