# 20 · 单智能体 Harness

> 所属层：1。目标：在 SWE-bench Verified（500 题）与 Terminal-Bench 2.x 上达到或超过官方分数，且不低于同模型下的 pi 与 Hermes。
> 实现方式：自有 Python 实现，压缩等算法**移植**自 pi（`@earendil-works/pi-coding-agent`，`dist/core/compaction/`）与 Hermes（`agent/context_compressor.py`，commit `1572836`），每处移植在代码注释中标出处。

## 1. 模块

| 模块 | 职责 |
|---|---|
| `harness/config.py` | harness 配置的 pydantic 模型与加载 |
| `harness/loop.py` | 状态机：一次激活从开始到结局 |
| `harness/transcript.py` | 会话记录（由事件折叠而来）与消息视图 |
| `harness/context.py` | 组装每次调用的消息列表 |
| `harness/compaction/` | 触发、切点、摘要、锚点索引、回退 |
| `harness/budget.py` | 费用、墙钟、步数计量与分级提醒 |
| `harness/tools/` | 内置工具与调度（并发与串行） |
| `harness/recovery.py` | 供应商错误、上下文超长、参数错误、循环检测 |
| `harness/outcome.py` | `finish` 工具与结局类型 |
| `bench/` | SWE-bench 与 Terminal-Bench 驱动 |
| `cli/trace.py` | `sciteam trace <run>` 回看一次运行 |

## 2. 配置（`configs/harness/*.yaml`）

```yaml
name: coder-default
model: deepseek-flash
reasoning_effort: max
system_prompt_ref: prompts/coder.md
tools: [bash, read_file, write_file, edit_file, recall, read_output, finish]
tool_output:
  max_chars: 30000          # 超出时保留头 40% + 尾 60%，全文存 BlobStore，可用 read_output 翻阅
  parallel_readonly: 8
budget:
  cost_usd: 50
  wall_clock_s: 10800
  max_steps: 3000           # 只作断路器
  warn_at: 0.70
  decide_at: 0.85
compaction:
  trigger_fraction: 0.5     # prompt_tokens > 0.5 × 上下文窗口
  trigger_abs_cap: 400000   # 与上式取较小者；在开发集上调参
  tail_fraction: 0.025
  tail_min: 10000
  tail_max: 25000
  tail_max_window_fraction: 0.20
  summary_model: null       # null = 与主模型相同
  summary_max_output: 16000
  anchor_budget_chars: 7000
  verbatim_user_budget_chars: 24000
  cooldown_s: [60, 300, 900]
recovery:
  provider_retries: 8
  backoff_base_s: 2
  backoff_max_s: 120
  loop_window: 6            # 最近 6 次调用中
  loop_repeat: 3            # 同一 (工具, 参数哈希, 结果哈希) 出现 3 次即判为循环
  empty_turn_retries: 2
sandbox:
  kind: docker
  network: none
  cmd_timeout_s: 1800
```

配置文件的摘要写入 `run_started` 事件（`10-kernel.md` ConfigStore）。所有数字只在配置中出现，代码中不写默认值以外的常量。

## 3. 循环状态机

```
INIT → ASSEMBLE → CALL_MODEL → DISPATCH_TOOLS → ASSEMBLE → …
                       │              │
                       ├→ RECOVERING ─┘（重试后回到 CALL_MODEL）
                       └→ COMPACTING → ASSEMBLE
DISPATCH_TOOLS 中执行 finish → FINISHED
预算或断路器触发 → FINISHED（budget_exhausted）
不可恢复错误 → FINISHED（error）
```

每一步写事件（流 `act:<activation_id>`）：

| 事件 | 载荷要点 |
|---|---|
| `activation_started` | 任务、harness 配置摘要、模型、沙箱镜像 |
| `model_called` | 请求消息列表的 BlobRef、工具列表摘要、参数 |
| `model_returned` | `AssistantTurn`（推理与正文存 Blob）、usage、费用、耗时 |
| `tool_started` / `tool_finished` | 调用 id、参数、结果 BlobRef、截断标记、耗时 |
| `compaction_started` / `compaction_finished` | 切点、被折叠的消息范围、摘要 BlobRef、摘要前后 token 数、方式（llm/fallback） |
| `budget_notice` | 级别（warn/decide/exhausted）与当前用量 |
| `recovery` | 错误类别、第几次重试、处理动作 |
| `activation_finished` | 结局、摘要、产物、声明、总用量 |

崩溃恢复：重启时读取该流，折叠得到会话记录，从最后一个完整步骤继续；没有 `tool_finished` 的副作用工具标记为「结果未知」并告知模型，不自动重放。`Harness.resume(stream)` 只恢复会话状态；沙箱由调用方提供。基准运行中容器随进程消失，因此基准驱动对崩溃实例整题重跑（每次尝试一个独立流 `swebench/<id>/<try_id>`），而不是续跑。

## 4. 上下文组装

每次调用的消息顺序固定（保持前缀稳定以命中缓存）：

1. `system`：系统提示 + **harness 契约**（工具用法、必须以 `finish` 结束、预算规则）。
2. `user`：固定信息（pins，例如仓库路径、测试命令）与任务描述。
3. 若已压缩：`user` 消息，内容为最新摘要（§5.4 格式），加锚点索引与原样保留的用户消息。
4. 保留尾部：切点之后的原始消息（assistant 含 `reasoning`、tool 结果）。
5. 需要时追加一条 `user` 通知：预算提醒、循环警告、shell 重启等。通知**只追加在末尾**，不修改历史，保持缓存前缀。

推理回传：模型目录中 `passback_with_tools: true` 时，保留尾部中所有 assistant 消息的 `reasoning` 原样回传。被折叠的轮次整体进入摘要，其推理随之移除——这不违反供应商要求，因为那些 assistant 消息也不再出现在请求中。切点不能落在某条 assistant 消息与其工具结果之间（§5.2），所以不会出现「有工具结果而缺对应 assistant 与推理」的情况。

预算行：harness 契约末尾有一行「已用 $x / $y，墙钟 a / b，步数 n」，由于它在前缀中会破坏缓存，**不放在系统提示里**，而是每 20 步或跨过提醒阈值时作为末尾通知追加。

## 5. 压缩

### 5.1 触发

- 以最近一次 `model_returned` 的 `usage.prompt_tokens` 为锚，加上此后新增消息的估算（字符数 / 4）得到 `context_tokens`（移植 pi `estimateContextTokens` 的做法：以供应商 usage 为准，只对增量估算）。
- 触发条件：`context_tokens > min(trigger_fraction × window, trigger_abs_cap)`。
- 被动触发：供应商返回 `ContextOverflow` 时立即压缩并重试同一次调用。

### 5.2 切点（移植 pi `findCutPoint`）

- 保留尾部预算 `tail = clamp(tail_fraction × window, tail_min, tail_max)`，且不超过 `tail_max_window_fraction × window`（Hermes 的 lean tail 规则）。1M 窗口下为 25K。
- 从末尾向前累加 token，直到超过 `tail`；从该位置向后找最近的合法切点。
- 合法切点只能在 user 消息或 assistant 消息之前；**不能**落在 tool 结果处。
- 若唯一合法切点位于一个很长的轮次中间（一条 user 之后几十次工具调用），按 pi 的 split-turn 处理：该轮次前半部分单独生成「轮次前缀摘要」（pi `TURN_PREFIX_SUMMARIZATION_PROMPT`），后半部分保留原样。
- 「轮次起点」只算真实输入（`task`、`incoming` 条目），harness 通知不算。任务描述固定保留在压缩区之外，所以单任务激活中不会出现 split-turn，总是走 §5.3 的普通/更新摘要；split-turn 只在第二层收到新消息时出现。
- 超过触发线但没有可折叠内容（如任务描述本身巨大）时，记 `compaction_skipped` 并继续，之后至少再追加两条消息才重试；只有供应商报 `ContextOverflow` 且无法压缩时才以 `error` 结束。

### 5.3 摘要生成

- 被折叠部分序列化为文本（pi `serializeConversation`：每条工具结果截到 2000 字符；pi 也序列化 thinking，这里同样保留，但每段截到 `serialize_thinking_chars`=2000 字符，因为 max 档推理很长而其中的计划与假设对续作有用）。
- 摘要提示词末尾附 Hermes 的「Detailed Session Log」段（保留标识符的逐步记录）。
- 摘要调用自身对 `RateLimited`/`Transient` 重试 3 次（对应 pi `retryAssistantCall`），仍失败才进入 §5.5 冷却。
- 首次压缩用 pi `SUMMARIZATION_PROMPT`；已有摘要时用 `UPDATE_SUMMARIZATION_PROMPT`，把旧摘要与新折叠部分合并，而不是摘要的摘要。
- 文件操作跟踪（pi `computeFileLists`）：从折叠部分的工具调用中提取读过和改过的文件，以 `<read-files>`、`<modified-files>` 附在摘要后，并跨多次压缩累积。
- 输出上限 `summary_max_output`。

### 5.4 摘要格式

沿用 pi 的节：Goal、Constraints & Preferences、Progress（Done / In Progress / Blocked）、Key Decisions、Next Steps、Critical Context。其后追加：

- **锚点索引**（移植 Hermes）：用正则从折叠部分提取文件路径、错误信息首行、提交哈希、分支名、URL、测试名，去重后在 `anchor_budget_chars` 内列出。
- **用户原话**（移植 Hermes）：被折叠范围内所有 user 消息原样保留，总长不超过 `verbatim_user_budget_chars`，超出时保留最早与最新。
- **恢复提示**：指明被折叠的条目范围，提示用 `recall(query=关键词)` 检索或 `recall(entry=n)` 取回原文。
- 摘要全文与被折叠条目范围 `[folded_from, folded_to)` 记入 `compaction_finished`；锚点索引和用户原话每次都在「全部已折叠条目」上重算，不会因多次压缩丢失早期标识符。

### 5.5 失败与回退（移植 Hermes 冷却梯度）

- 摘要调用失败或输出为空：进入冷却 60 秒，再次失败 300 秒，之后 900 秒；冷却期间使用**确定性回退摘要**：锚点索引 + 用户原话 + 文件列表 + 每轮一行「工具名(关键参数) → 退出码/结果首行」。
- 回退摘要也写 `compaction_finished`，`method=fallback`，便于统计。
- 压缩后 `context_tokens` 仍超过触发线：将尾部预算减半重试一次，仍不行则以 `error` 结局结束，并在事件中写明原因。不允许静默丢消息。

## 6. 预算

- 三项计量：费用（按 `11-providers.md` §1.4，区分缓存命中）、墙钟、步数。任一达到上限即触发。
- 70%：追加通知「已用 70%，请规划收尾」。
- 85%：追加通知并要求本轮决定：继续（说明剩余计划）或立即 `finish`。
- 100%：不再执行工具；向模型发一次收尾调用，结局为 `budget_exhausted`，这次调用不计入断路器。模型支持强制 `tool_choice` 时指定 `finish`；DeepSeek thinking 模式不支持（2026-09-23 实测 `required` 与指定函数均返回 400，见 `models.yaml` 的 `forced_tool_choice`），此时工具列表只给 `finish` 并追加通知；模型仍以纯文本回复时，把正文作为交接摘要。
- 摘要生成的费用计入同一预算。

## 7. 结局

`finish(outcome, summary, artifacts, claims)`：

- 若 `finish` 与其他工具调用出现在同一轮且其中有调用失败，拒绝本次 `finish`（模型尚未看到那些结果）。

- `outcome ∈ {completed, blocked, need_input, handoff}`；`budget_exhausted` 与 `error` 只能由 harness 产生。
- 模型在不调用工具的情况下给出纯文本回复：追加通知「请用 finish 结束或继续使用工具」，最多 `empty_turn_retries` 次；仍不调用则以 `error`（`reason=no_finish`）结束。**不存在没有结局的激活**。
- 基准驱动可在 `finish` 前执行检查（例如 SWE-bench 要求 `git diff` 非空），检查失败时把结果作为工具结果返回，模型继续工作。
- 完成复核（`finish_reviews`，默认 0）：前 N 次 `finish(completed)` 不被接受，工具结果返回复核指令并附上任务原文。复核指令要求对照当前实际状态逐条核验显式要求（名称与路径、格式、键名、数值、限制、需运行的服务），并独立重算报告中的数字，然后再次调用 finish。
  - 复核次数由 `tool_finished.meta.finish_review` 折叠得到，续跑后不会重复复核。
  - 预算已达 `decide_at` 或结局不是 completed 时跳过复核；收尾调用不走复核。
  - 动机：TB 开发集基线中 9 个失败 trial 全部以 completed 提前结束（用掉预算的 3–55%），失败原因是字面要求不符（键名 `man_in_middle` 写成 `man_in_the_middle`）或报告数值未经独立核算。
  - 是否默认启用由开发集 A/B 决定（`terminal-default` 对 `terminal-review`，同 67 题各 3 次）。

## 8. 工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `bash(command, timeout_s?)` | 副作用 | 持久 shell（`11-providers.md` §2.2） |
| `read_file(path, offset?, limit?)` | 只读 | 带行号输出，默认 2000 行 |
| `write_file(path, content)` | 副作用 | 整文件写入 |
| `edit_file(path, old, new, replace_all?)` | 副作用 | `old` 必须唯一匹配，否则报错并给出匹配数与各处行号 |
| `recall(ref)` | 只读 | 取回被折叠的原始消息 |
| `read_output(ref, offset, limit)` | 只读 | 翻阅被截断的工具输出全文 |
| `finish(...)` | 控制 | §7 |

调度：同一轮中的只读调用并发（上限 `parallel_readonly`）；副作用调用按模型给出的顺序串行执行，**全部执行**，某条失败不阻止后续条目，每条的结果单独返回。

参数错误：`arguments=None` 或不符合 JSON Schema 时，不执行，把解析错误与 schema 作为工具结果返回。

## 9. 恢复

| 情况 | 处理 |
|---|---|
| `RateLimited` / `Transient` | 指数退避（`backoff_base_s` 起、上限 `backoff_max_s`），最多 `provider_retries` 次；超过则 `error` |
| `ContextOverflow` | §5.1 被动压缩后重试 |
| `finish_reason=length` | 若已有工具调用，按已有部分执行；否则追加通知「输出被截断，请分步」后重试 |
| 空回复 | 重试最多 `empty_turn_retries` 次 |
| 循环 | 最近 `loop_window` 次调用中同一 `(工具, 参数哈希, 结果哈希)` 出现 `loop_repeat` 次：追加警告；再次出现则强制进入 85% 决策轮 |
| 沙箱命令超时 | 结果标注超时并告知 shell 已重启 |
| 沙箱容器死亡 | 重建容器一次；工作区改动丢失时如实告知模型 |

## 10. 技能与记忆（第一层范围）

- 技能：`skills/<name>.md`，harness 配置列出可用技能，系统提示只放名称与一句话描述；模型用 `read_file` 读取全文。
- 记忆：第一层只有激活内的会话记录与 `handoff` 摘要。跨激活记忆属于第二层（组织运行时）。

## 11. 基准驱动

### 11.1 SWE-bench Verified

- 镜像：`swebench/sweb.eval.x86_64.<id 中 __ 替换为 _1776_>:latest`（已预拉取，见 `bench/swebench/pull_images.py`）。
- 容器内工作目录 `/testbed`，shell 启动时执行 `source /opt/miniconda3/bin/activate testbed`。
- 任务提示：问题描述原文 + 「修改 /testbed 中的非测试代码使问题得到解决；可以编写并运行复现脚本；完成后调用 finish」。不提供 `FAIL_TO_PASS` 测试名（与官方设置一致）。
- 结束时取 `git add -A && git diff --cached HEAD` 作为补丁，再由 `drop_new_artifacts` 剔除新增的构建与缓存产物（`build/`、`dist/`、`*.egg-info/`、`__pycache__/`、`*.py[co]`、`.eggs/`、`.tox/`、`.pytest_cache/`）。已跟踪文件的改动与新增源文件原样保留。复现脚本按提示放在 /tmp，不进入补丁。2026-09-26 的 Verified 运行中有 1 题因混入 `build/lib/**` 打补丁失败，此后加入该规则（单测覆盖）。
- 评测：官方 `swebench.harness.run_evaluation`，**锁定 swebench 4.1.0**（5.x 要求数据集自带 `image`/`eval_script` 字段，与 Verified 原始数据不兼容），必须带 `--cache_level instance`，否则评测器会删除预拉取的实例镜像。入口：`sciteam swebench-eval <run_dir>`。2026-09-23 用 gold 补丁在 `astropy__astropy-12907` 上验证评测链路（resolved）。
- 提示词中禁止在包缓存、其他安装副本或网络中寻找含修复的新版本（冒烟中观察到模型翻 pip 缓存），这是诚信约束，不属于调参。
- 结果有效性：`results.jsonl` 只追加，每题以最后一条记录为准。`driver_error` 与 `reason ∈ {provider_quota, provider_unavailable, auth}` 属于基础设施失败，续跑时重跑，也不写入 `preds.jsonl`。
  - 出现 `provider_quota` 或 `auth` 时熔断：不再启动新题，已在跑的题自然结束。
  - pi / Hermes runner（`bench/baselines/common.py`）规则相同，另外会检查 agent 自己的 stdout/stderr 里有没有计费报错。
  - 磁盘：运行目录或 Docker 数据目录可用空间低于 `--min-free-gb`（默认 50）时同样熔断，拉镜像前和开题前都检查。2026-09-26 夜间 Verified 镜像把磁盘写满，导致所有运行中断。
  - 单题收尾（删容器、关连接）失败只记警告，不丢该题结果，也不中断其他题：Docker 繁忙时 `docker rm` 曾超过 120 s，把整个运行带崩。
- 报告：解决率、每题费用/墙钟/步数/压缩次数、结局分布；与 pi、Hermes 同模型同题目对比。
- 调参只在开发集上进行（SWE-bench 非 Verified 部分、SWE-Gym），Verified 只跑最终配置。
- 历史泄漏：部分 SWE-bench 镜像保留了基准提交之后的上游历史（tag、其他 ref、克隆时的 reflog），`git log --all` 就能看到参考修复。2026-09-27 在 dev 镜像 django-11754 上确认（HEAD 祖先 27804 个提交，全部 ref 可达 48985 个，参考修复 `13e4abf83e` 可读），且 agent 实际读到了它。驱动在 agent 开始前执行 `HISTORY_SCRUB`（`sciteam/bench/swebench.py`，基线共用）：只保留当前分支，删 tag、remote、reflog，`gc --prune=now`，并断言可达提交数等于 HEAD 祖先数，否则该题按驱动错误处理。Verified 抽样（每仓库一题）核查结果见路线图 9.5。

### 11.2 Terminal-Bench 2.x

- 官方框架为 Harbor（已装 harbor 0.23.0），数据集 `terminal-bench/terminal-bench-2-1`。驱动 `sciteam/bench/terminal_bench.py:SciTeamAgent` 是 Harbor 外部 agent（`BaseAgent` 子类，`options_model` 接收 `--ak harness=… model=…`）。
- 沙箱：Harbor 以 docker compose 启动任务容器（project = 规整后的 `session_id`，服务 `main`）。驱动按 compose 标签找到 main 容器，用 `DockerSandbox` 的附着模式（`attach_container`）接上同一套 `docker exec -i` 持久 shell；user、env 取自 Harbor 环境（`default_user`、`_merge_env`，含 trial 作用域变量）。附着模式下容器不归我们所有：关闭时只杀自己的 shell，不删容器，不杀 agent 留下的进程（验证器可能依赖它们）。
- 超时语义：附着模式不能 `kill -9 -1`（会杀掉任务服务）。shell 启动时记录自身 PID，每条命令前记录容器 uptime；超时时只杀「本 shell 后代 ∧ 同 session ∧ 启动时间晚于本条命令开始」的进程。更早命令起的后台进程、`setsid` 脱离的进程保留。只依赖 bash 内建与 `/proc`（任务镜像可能没有 ps/pgrep）。live 测试覆盖。
- 墙钟预算：Harbor 不把超时传给 agent。驱动读取 `<task>/task.toml` 的 `agent.timeout_sec`（乘以 job 配置中的倍率，默认 1），设 `budget.wall_clock_s = timeout − max(90 s, 10%)`，让收尾调用在 Harbor 取消前完成。排行榜不允许改超时。
- 配置：`configs/harness/terminal-default.yaml` + `configs/prompts/terminal.md`（通用终端任务提示：按字面满足要求、服务要脱离启动、结束前逐条核验）。命令默认超时 300 s。
- 用量回填 `AgentContext`（输入/缓存/输出 token、费用、结局元数据），放在 finally 中，被 Harbor 超时取消时同样回填。事件库写在 trial 的 `agent/events.db`，`sciteam trace <trial>/agent` 可查看。
- 运行：`harbor run -d terminal-bench/terminal-bench-2-1 --agent sciteam.bench.terminal_bench:SciTeamAgent --ak model=deepseek-flash -k 5 -n <并发> -r 3 --retry-include EnvironmentStartTimeoutError --retry-include SandboxBuildFailedError`。排行榜要求每题至少 5 次。只对环境类错误重试：并发构建时环境启动会超过 Harbor 的 300 s 启动上限，这类失败发生在 agent 开始之前。`AgentTimeoutError` 属于真实失败，不重试。
- 2026-09-24 链路验证：oracle 在 write-compressor 上 reward 1.0；flash 在同一题上 reward 1.0（40 步，$0.057，6.8 分钟，预算 810 s）。
- 有效性：TB 验证器在打分时联网安装依赖，断网会直接判 0 分而不跑测试。`bench/tb/analyze.py` 把这类 trial（以及环境启动/构建失败、奖励文件缺失、验证器超时）标为 infra，不计入分母；正式报告同时给出 infra 数量，并对 infra trial 另开 job 重跑。
- 开发集：Terminal-Bench 2.1 本身不调参。调参用与 2.1 不重叠的 Harbor 数据集，先核对题目名去重。
- 报告格式与 11.1 相同。

### 11.3 pi 与 Hermes 基线

- 同模型、同推理档位、同预算上限、同镜像；各自使用默认 harness 配置。
- 驱动脚本放在 `bench/baselines/`，输出同一格式的补丁文件，由同一评测器打分。

## 12. 追踪 CLI

`sciteam trace <run_id> [--activation id] [--full]`：按时间列出调用、工具、压缩、预算通知和结局；`--full` 展开 Blob 内容；`verify` 子命令检查哈希链。

## 13. 测试清单

单元：

- 切点从不落在 tool 结果上；split-turn 的前缀与后缀拼接后覆盖原消息范围且不重叠。
- 保留尾部 token 数在 `[tail_min, tail_max]` 之内（窗口足够大时）。
- 更新摘要使用 UPDATE 提示，且文件列表跨两次压缩累积。
- 锚点索引与用户原话不超过各自字符预算。
- 摘要失败时按 60/300/900 冷却，期间使用回退摘要。
- 压缩后请求中每条 tool 消息都有对应的 assistant 消息，且该 assistant 消息带推理。
- 预算三个阈值各触发一次通知；100% 时只允许 `finish`。
- 无 `finish` 的纯文本回复最终以 `error(no_finish)` 结束。
- 副作用工具串行且全部执行；只读工具并发数不超过上限。
- 循环检测在第 3 次重复时告警。

集成（`ScriptedLlm` + `LocalSandbox`）：

- 50 轮长会话触发两次压缩，`recall` 能取回第一次被折叠的原始消息。
- 在任意步骤杀进程后重启，继续执行，事件流哈希链完整。
- 离线回放（`RecordingLlm` 录制的真实会话）得到相同的事件序列。

真实（需密钥与镜像）：

- SWE-bench Verified 随机 20 题冒烟，零无结局激活，每题至少一次事件完整。
- 最终验收：见本文开头目标。
