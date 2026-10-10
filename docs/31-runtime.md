# 31 · 运行时：调度器、激活、静止检测

> 所属层：2。依赖 `kernel`、`providers`、`harness`、`comm`。
> 目标：agent 是由事件唤醒的 actor。**没有轮次**：谁有注意项谁被唤醒；同一 agent 的激活串行，不同 agent 并发；一个 agent 做几个小时的任务，不妨碍其他 agent 接新活；长任务进行中收到的消息在下一步送达，而不是等它结束。

## 1. 运行（Run）

一次团队运行 = 一个事件库 `runs/<run>/events.db` + BlobStore + 共享工作区 `runs/<run>/workspace/`。

```python
class RuntimeConfig(BaseModel):          # configs/runtime/*.yaml
    max_concurrent_activations: int      # 全局并发上限
    debounce_s: float                    # 首个注意项到达后等待合并的时间
    memory_recent_activations: int       # 简报中带入的最近激活摘要条数
    briefing_messages_per_conv: int      # 简报中每个会话最多展示的新消息数
    deliver_during_activation: bool      # 激活进行中是否送达新的唤醒项
    rate: RateConfig                     # §6
    budget: RunBudget                    # cost_usd、wall_clock_s：整次运行的断路器
    on_quiescent: Literal["stop", "wait"]  # 批处理运行 stop；交互运行 wait 人类输入
    error_retries: int                   # 激活以 error 结束后的重试次数
    error_backoff_s: list[float]
    sandbox: TeamSandboxConfig           # §7
```

`run_started` 事件记录团队文件、通信策略、运行时配置、各 agent 的 harness 配置与提示词的内容摘要（`ConfigSnapshot`）及初始目标。

## 2. 注意项（Attention）

注意项是「某成员应当关注的某条事件」。它不是新存储的数据，而是由纯函数从领域事件推导：

```python
def attention_for(ev: Event, view: CommView, policy: CommPolicy) -> list[AttentionItem]

class AttentionItem(BaseModel):
    agent: str
    kind: Literal["dm", "mention", "room", "human", "thread_reply", "task_assigned",
                  "task_ready", "task_open", "task_review", "task_returned", "task_changed",
                  "timer", "continue", "retry"]
    seq: int            # 来源事件的全局 seq
    wakes: bool         # kind ∈ policy.wake_on
```

| 来源事件 | 注意项 |
|---|---|
| `message_posted` 于 dm | 对方：`dm` |
| `message_posted` 含 mention | 每个被 mention 的 agent：`mention` |
| `message_posted` 于 room/task | 其他成员：`room`；作者是人类时为 `human`。两者默认都不在 `wake_on` 中：只有 mention 才唤醒，其余进入下一次简报的摘要（C3） |
| `message_posted` 回复某线程 | 该线程中发过言的成员：`thread_reply` |
| `task_status_changed` assign/claim | 负责人：`task_assigned`；任务尚不能开始（启动类依赖未满足）时不唤醒，等 `task_ready` |
| 新建或退回为 open 且可开始的任务 | 策略允许认领的 agent：`task_open` |
| 依赖变为满足 | 负责人：`task_ready` |
| submit（有评审人） | 评审人：`task_review` |
| reject / reopen | 负责人：`task_returned` |
| 其他状态变化 | 创建者与关注者：`task_changed` |
| `timer_fired` | 设定者：`timer` |
| 激活以 `handoff` 结束 | 本人：`continue` |
| 激活以 `error` 结束且未超重试 | 本人：`retry` |

作者本人不产生注意项。人类成员的注意项进入人类收件箱（CLI/Web 展示），不触发激活。

**送达记录只有一个来源**：激活的 `activation_started.meta.attention` 与 `incoming_added.meta.attention` 列出本次送达的注意项 seq。某成员「已送达集合」= 其全部激活流中这些字段的并集。这样崩溃时不会出现「标记送达但未进入上下文」的不一致。

## 3. 调度器

### 3.1 成员状态（由事件折叠）

```
idle ──(有唤醒项, 过 debounce)──▶ running ──finish──▶ idle
                                  │                    ├─ need_input: 等新唤醒项
                                  │                    ├─ handoff: 自动产生 continue
                                  │                    └─ error: retry（退避）或 faulted
faulted ──(人类 @ 或 reset)──▶ idle
```

### 3.2 主循环

```
subscribe(events, after=cursor)
for ev in stream:
    view.apply(ev)
    for item in attention_for(ev, view, policy):
        pending[item.agent].append(item)
    tick()

tick():
    for agent in idle agents with a waking pending item, ordered by earliest item seq:
        if running_count >= max_concurrent: break          # 其余排队，不丢
        if rate limiter denies: schedule retry at allowed time; continue
        if now < first_waking_item.ts + debounce_s: schedule tick; continue
        start_activation(agent, pending[agent])            # 取走全部未送达项
    if quiescent(): on_quiescent()
```

- 激活协程异常退出而没有写结局（例如沙箱启动失败）时，调度器补写 `activation_finished{outcome: error, reason: runner_error}`，保证每次激活都有结局。
- 调度器自身的判断都由事件折叠得到：`pending` 是「注意项 − 已送达集合」，成员状态是激活流的最后结局。调度器进程重启后重新折叠即可恢复。
- 调度器不调用模型、不做任何领域判断；它只决定**何时**为**谁**开激活。先做什么、派给谁，是 agent 按项目管理方式自己决定的（看板、依赖、关键路径都以工具形式提供给它们）。

### 3.3 激活的输入（简报）

激活的任务文本由 `briefing.py` 渲染，结构固定以利于缓存：

1. **你是谁**：名册条目、角色说明（profile）、所在团队与上级/下级。
2. **规则**：当前通信策略的自然语言摘要，以及 `directory()` 中「你可以联系谁、可以做什么」。
3. **目标**：运行的初始目标（团队文件或 `sciteam team start --goal`）。
4. **你的任务**：自己负责、评审、创建的未完成任务（状态、就绪情况）。
5. **记忆**：最近 `memory_recent_activations` 次激活的 finish 摘要（时间、结局、摘要）。成员属性 `memory_activations` 可以按成员覆盖这个条数。设为 `"0"` 时每次激活都不带以往摘要（无记忆激活，用于孤立重核，60 §13.2 G2）。它能读到哪些会话仍由通信策略决定。目前只能在团队文件的成员 `attrs` 里设置，manifest 席位还不能设置。
6. **新动态**：本次送达的注意项，按会话分组渲染（作者、时间、msg_id、线程），每会话最多 `briefing_messages_per_conv` 条，多出的给出 `read_conversation` 提示；不唤醒的房间消息只给计数与最近几条。
7. **收尾说明**：处理完新动态后以 `finish` 结束；需要别人答复用 `need_input`；工作未完需继续用 `handoff`。

系统提示 = agent 的 harness 提示词 + profile；工具 = harness 配置中的工具 + 通信工具（按 harness 配置 `tools` 列表启用）。

### 3.4 激活进行中的送达

`deliver_during_activation: true` 时，harness 在每次模型调用前调用注入的 `poll_incoming()`；调度器把该 agent 新到的唤醒项渲染为一条 `incoming_added` 条目（「[新消息] …」），追加在上下文末尾，不改历史，保持缓存前缀。非唤醒项不打断，留到下一次简报。

这正是 harness 设计里 `incoming` 条目与 split-turn 压缩的用途（20 §5.2）。

### 3.5 结局处理

| 结局 | 调度器动作 |
|---|---|
| `completed` | idle |
| `need_input` | idle，等下一个唤醒项；若摘要中 mention 了人，由 agent 自己在结束前发消息，调度器不代发 |
| `blocked` | idle。按团队提示词，agent 在结束前自己告诉能解除阻塞的人；调度器不代发消息（系统不是会话成员，也不替 agent 说话） |
| `handoff` | 产生 `continue` 注意项（受速率约束），下一次激活的简报带上本次摘要 |
| `budget_exhausted` | 同 handoff，但计入 `rate.max_consecutive_handoffs` |
| `error` | 按 `error_backoff_s` 产生 `retry`；超过 `error_retries` → faulted，并通知 `@human` |

## 4. 静止与停止

- **静止**：没有运行中的激活；没有 agent 有未送达的唤醒项（含处于 debounce 或速率等待的）；有非终态任务时没有未到期的定时器（所有任务都已关闭后，之前设下的提醒视为过期，不再阻止静止；监控长作业时对应任务仍在进行中，定时器照常生效）。
- 静止时写 `run_quiescent{open_tasks, faulted, cost_usd}`（`sciteam team status` 展示看板）。之后：
  - `on_quiescent: stop`：写 `run_stopped{reason: quiescent}`，进程退出；
  - `on_quiescent: wait`：等待人类消息（人类消息是唤醒项），可无限期等待。
- **停止**：人类 `sciteam team stop`（写 `run_stop_requested`）→ 不再开新激活，运行中的激活取消（harness 记 `error/cancelled`），写 `run_stopped{reason: human}`。
- **继续**：对已停止的运行执行 `sciteam team resume` 写 `run_resumed{after}` 并照常调度：停止后到达的人类消息、审批结果等唤醒项得到处理，没有新事则立即再次静止。断路器仍处于触发状态时不继续，需先 `sciteam team reset --breaker`。
- 第四层的目标停机谓词（O4）作为另一个停止来源接入同一出口。

## 5. 定时器

`schedule_wakeup(after_s, note)` 写 `timer_set{timer_id, agent, due_ts, note}`；调度器在到期时写 `timer_fired`，产生 `timer` 注意项。用途：长任务的定期检查、等待外部作业。有未到期定时器时系统不算静止。定时器上限 `rate.max_timers_per_agent`。

## 6. 速率限制与断路器

```yaml
rate:
  activations_per_agent_per_hour: 30
  max_consecutive_handoffs: 20        # 连续 continue 激活的上限（防止无限自续）
  max_timers_per_agent: 5
  storm:
    window_s: 600
    max_messages: 200                 # 窗口内全队消息数
    max_messages_without_progress: 80 # 窗口内无任务状态推进的消息数
```

- 激活频率超限：该 agent 的唤醒项排队到允许时刻，不丢弃。
- 消息速率（每 agent、每对 agent）在 `PolicyGate` 执行，超限的 `post_message` 被拒并告知何时可再发（30 §7.1 `limits`）。
- **风暴断路器**：窗口内消息数超限，或「无进展消息数」超限（窗口内晚于最近一次进展事件 `task_created`/`task_status_changed` 的消息数；早先的一次进展不能让之后的闲聊一直免检） → 写 `breaker_tripped{kind: storm}`，暂停所有新激活；交互运行（`on_quiescent: wait`）由人类 `sciteam team reset --breaker` 恢复，批处理运行（`stop`）直接进入停止流程。断路器触发视为缺陷，需要在报告里解释（00 §3）。
- **预算断路器**：运行累计费用或墙钟超过 `budget` → `breaker_tripped{kind: budget}`，按停止流程处理。

## 7. 沙箱与工作区

- 每个 agent 一个持久容器（整次运行复用），共享工作区以读写方式挂载到 `/workspace`。镜像、网络、资源由 `TeamSandboxConfig` 指定；`user: auto` 以宿主用户身份执行命令，工作区文件不会变成 root 所有。验收镜像 `sciteam-team:py312`（`bench/team/Dockerfile`：python 3.12、git、pytest、ruff，无网络）。
- 并发写同一文件是 agent 之间要协调的事（和人一样）；工作区初始化为 git 仓库，简报提示用分支或按任务划分目录。运行时不加文件锁。
- **私有工作区**（`sandbox.private_mount`，默认关闭）：设为某个路径（如 `/private`）后，成员 x 的私有目录 `<run>/private/x` 在每个挂载它的容器里都位于 `<private_mount>/x`。x 自己读写挂载；默认其他成员的容器不挂载它，所以彼此读不到。验证沙箱把全部私有目录只读挂载在同一路径下。同一个文件在所有视角下路径相同，所以验收命令可以直接引用（如 `test -s /private/prover1/results/k.md`），也可以比较多名成员的产物（如独立重复，60 §6.6）。只隔离文件；对话是否可见仍由通信策略决定，共享的 `/workspace` 照常对所有人可见。`LocalSandbox` 做不到隔离，配置了 `private_mount` 时直接报错。
- **按成员的文件权限**（成员 `attrs`，60 §13.2 G3）：
  - `workspace_access: ro`：共享 `/workspace` 只读挂载。用于「只能在私有目录交付」的成员，防止未验证的结论经工作区文件或 git 历史流到别人那里。
  - `reads_private: a,b`：把所列成员的私有目录只读挂载到同一路径 `<private_mount>/<id>`。读权限要由读者一方声明，未被列出的成员看不到。需要 `private_mount`。
  - 在 `LocalSandbox` 上设置这两项会直接报错，不会静默放宽。已用真实 Docker 验证：只读挂载拒绝写入，未授权成员看不到别人的目录。
- 测试用 `LocalSandbox`（临时目录）。

## 8. 崩溃恢复

重启 `sciteam team resume <run>`：折叠事件得到名册、会话、看板、成员状态、已送达集合、定时器；对有 `activation_started` 而无 `activation_finished` 的激活调用 `Harness.resume`（20 §8）；其余照常调度。每个运行同一时刻只允许一个调度进程：`run_team` 对 `<run>/scheduler.lock` 加排他 `flock`，已被占用时拒绝启动（`RunLocked`）；`post`/`stop`/`status` 只追加或读取事件，不受锁限制。

## 9. 人类接口（CLI）

| 命令 | 作用 |
|---|---|
| `sciteam team start <team.yaml> --run <dir> [--goal "…"]` | 写名册、房间、预置任务、开场消息（`kickoff`，由人类成员经 Gate 发出）与目标，启动调度器 |
| `sciteam team post <run> --as <human> --to @x|#room|task:<id> "…"` | 人类发消息（与 agent 走同一个 Gate） |
| `sciteam team status <run>` | 成员状态、看板、未读、断路器 |
| `sciteam team stop|resume <run>` | 停止 / 从断点恢复 |
| `sciteam team reset <run> [--breaker] [--agent x]` | 解除断路器 / 解除 agent 的 faulted 状态 |
| `sciteam trace <run> [--agent x] [--conv c]` | 回看激活与会话 |

Web 界面（70）只读事件库并调用同一组服务函数。

## 10. 模块

| 模块 | 职责 |
|---|---|
| `runtime/config.py` | RuntimeConfig、TeamFile 加载 |
| `runtime/attention.py` | `attention_for`、已送达集合折叠 |
| `runtime/briefing.py` | 简报与激活中新消息的渲染 |
| `runtime/scheduler.py` | 成员状态、主循环、debounce、速率、静止、定时器、断路器 |
| `runtime/agent.py` | 为某成员构造 Harness（配置、提示词、工具、沙箱、`poll_incoming`）并运行/续跑激活 |
| `runtime/team.py` | 运行的创建、恢复、停止；组合根 |

## 11. 测试清单

- 单元（假 LLM `ScriptedLlm`、假时钟、本地沙箱）：
  - `attention_for` 对每类事件的输出；作者不收到自己的注意项；
  - 被 mention 者被唤醒、未被 mention 的房间成员不被唤醒（C3）；
  - debounce：窗口内三条私聊合并成一次激活；
  - 同一 agent 串行：运行中到达的唤醒项在下一步以 `incoming_added` 送达，不另开激活；
  - 并发上限：N 个 agent 同时被唤醒，同时运行数不超过上限，其余排队后全部运行；
  - 长任务不阻塞：A 的激活挂起时，B、C 的多次激活照常完成；
  - 结局处理：handoff → continue；error → retry → faulted；need_input 等待；
  - 静止检测：无消息时 `run_quiescent` + 停止；有未到期定时器且有未关闭任务时不静止，任务全部关闭后过期定时器不阻止静止（C6）；
  - 风暴：两个脚本 agent 互相私聊 → 被消息速率拒绝 / 断路器触发（C6）；
  - 崩溃恢复：在激活中途中断调度器，重启后折叠状态一致、激活续跑、注意项不重复送达。
- 集成：脚本化的四人团队在五份策略下完成同一预置任务流，事件序列满足各策略的可达性约束。

## 12. 真实模型验收（C1–C6）

验收任务要能客观打分，并需要真实分工：

- **任务 `minipkg`**：实现一个小型 Python 包（带 CLI 的 INI/TOML 子集配置解析与合并工具，约 6 个模块），规格文档给出公开接口与行为；**隐藏测试**（约 60 条）在运行结束后由驱动在工作区执行，得分 = 通过率。规格足够大，一个人做完需要多次激活，分工有收益。
- **团队**：1 lead + 3 worker + 1 reviewer + 1 human（owner，只下达目标、不干预），模型 deepseek-flash（max 推理），之后换 pro 复核。
- **运行矩阵**：五份策略（flat、hierarchical、workflow、wave、market）各跑一次；workflow 与 wave 的预置任务 DAG 放在各自团队文件里。
- **每次运行的判定**（全部从事件库自动检查，写入报告）：
  - C1：出现私聊，且非成员的读取请求被拒（若有）；
  - C2：出现房间消息与线程回复；
  - C3：每次激活都有唤醒它的注意项；被 mention 者在 debounce + 调度延迟内被激活；未被唤醒者无激活；
  - C4：任务从 open 走到 done；非法迁移的拒绝记录；
  - C5：策略禁止的 Act 全部被拒，允许集之外没有领域事件；不同策略下通信图（谁和谁说过话）不同且符合策略；
  - C6：运行以 `run_quiescent` 结束，无风暴断路；
  - 结果：隐藏测试通过率、费用、墙钟、消息数、激活数，五模式对比表。
- 另做一个 C3/C6 定向小场景的真实运行（三人房间里只 mention 一人；两人被要求互相确认的乒乓任务），证明唤醒和限流在真实模型下起作用。
