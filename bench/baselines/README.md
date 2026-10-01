# SWE-bench baselines：pi 与 Hermes

两个 runner 与我们自己的 harness（`sciteam/bench/swebench.py`）尽量同条件：同一官方镜像
`swebench/sweb.eval.x86_64.<id>:latest`、同样的容器参数（`--network none --memory 16g --cpus 4`、
workdir `/testbed`、`git config --global --add safe.directory /testbed`），同样的任务文本
（`TASK_TEMPLATE`，只把末句 “call finish.” 换成 “stop.”，因为两个 agent 都没有 finish 工具，靠停止调用工具结束），
patch 用同一条 `cd /testbed && git add -A && git diff --cached --no-color HEAD` 取。
模型参数：DeepSeek OpenAI 兼容接口，`thinking: enabled`，`reasoning_effort: max`，模型取自 `configs/models.yaml`（只读）。
上限：单题墙钟 3h（`--wall-limit-s`），步数 3000（`--max-steps`，只有 pi 能强制，见下文）。

```bash
# pi
.venv/bin/python bench/baselines/run_pi.py --ids-file X --run-dir runs/pi_xxx --concurrency N --model deepseek-flash
# Hermes（--audit-requests 可选：落盘每次请求的 thinking/effort/reasoning_content 回传统计，不含消息内容）
.venv/bin/python bench/baselines/run_hermes.py --ids-file X --run-dir runs/hermes_xxx --concurrency N --model deepseek-flash
# 评测（必须带 --cache_level instance，否则评测器会删掉预拉的镜像）
cd runs/xxx && ../../.venv/bin/python -m swebench.harness.run_evaluation \
  --dataset_name ../../bench/swebench/verified.jsonl --predictions_path preds.jsonl \
  --max_workers 1 --run_id xxx --cache_level instance --report_dir .
```

输出：`patches/<id>.diff`、`results.jsonl`（status / wall_s / steps / tokens / cost / error）、`preds.jsonl`、
`logs/`（agent 事件流与 stderr）、`sessions/`（agent 自己的会话记录）。可断点续跑：`results.jsonl` 里已有且
status 不是 `infra_error` 的实例会被跳过。容器都带 `sciteam.baseline=<agent>` 标签，单题结束和整批结束都会删除。
status 取值：`completed` / `agent_error`（agent 报错退出，patch 照样提取）/ `wall_timeout` / `step_limit` / `infra_error`。
`cost_usd` 是按**高峰价**算的上限（agent 不记录每次调用的时间），token 数是准的。

## pi（`@earendil-works/pi-coding-agent` 0.84.1，安装在 ATLAS `paper/lab/tools/pi/`，可用 `PI_PKG` 覆盖路径）

- **运行方式**：宿主上执行 `node dist/cli.js -p --mode json`（print 模式，非交互），默认系统提示词、默认 compaction
  （`contextWindow - 16384` 时触发）、默认工具集 `read,bash,edit,write`；用隔离的 `PI_CODING_AGENT_DIR`
  （只放一个 `models.json`，自定义 provider `sciteam-deepseek` 指向 `https://api.deepseek.com`，
  `thinkingFormat: deepseek`、上下文 1,048,576、max_tokens 393,216），宿主 cwd 是一个空目录，关掉了
  extension/skill/上下文文件的自动发现（宿主上没有这些东西，关掉是为了不受宿主 `~/.pi` 影响）。
- **命令怎么到容器**：扩展 `pi/docker_tools.ts` 用 pi 官方的 `create*ToolDefinition(cwd, {operations})` 接口
  （与 pi 自带 `examples/extensions/ssh.ts` 相同的机制）替换四个内置工具的 I/O：`read`/`write`/`edit` 走
  `docker exec cat` / `cat >`，`bash` 走 `docker exec setsid bash -c "source /opt/miniconda3/bin/activate testbed; cd <cwd>; <cmd>"`。
  工具定义、描述、截断逻辑都是 pi 原样的；超时/中断时按进程组在容器内 kill。宿主环境变量不传进容器。
  pi 会把被截断的 bash 输出存到宿主临时文件并把路径告诉模型，扩展会把这个文件 `docker cp` 到容器里同一路径，
  模型用哪个工具都能读到。系统提示词里的 “Current working directory” 改写成 `/testbed`。
- **reasoning_content 回传**：pi 原生支持。`openai-completions` 适配器把 thinking 块以 `reasoning_content` 字段回放；
  没有产生 thinking 的轮次会补空字符串。实测（`logs/<id>.pi.payload_audit.jsonl`，扩展通过
  `before_provider_request` 记录每次请求的 thinking/effort/每条 assistant 消息有没有 reasoning_content）：
  每次请求都是 `thinking={type:enabled}`、`reasoning_effort=max`、从不缺失 `reasoning_content`，没有出现 400。
  `deepseek-flash` 和 `deepseek-v4-pro` 都接受空字符串占位（2026-09-24 冒烟测试）。
- **步数**：pi 本身没有步数上限；runner 从 JSON 事件流里数 assistant 消息，达到 `--max-steps` 就终止（`step_limit`）。

## Hermes（`third_party/hermes-agent` @ 1572836，装在 `bench/baselines/.hermes-venv`，`pip install -e`）

- **运行方式**：宿主上执行 `hermes -z <task> --provider deepseek -m <model> --reasoning max --toolsets ... --usage-file ...`
  （官方 one-shot 模式：与 CLI 聊天轮次同样构造 `AIAgent`，自动跳过审批，stdout 只输出最终回答）。每个实例一个全新
  `HERMES_HOME`（`config.yaml` 指定 deepseek provider、`context_length` 1,048,576、docker 终端后端），宿主 cwd 为空目录。
  Hermes 自己的系统提示词、compaction、记忆、todo、delegation 等保持默认。
- **命令怎么到容器**：用 Hermes 自带的 `docker` 终端后端（`terminal.backend: docker`、`cwd: /testbed`、
  `docker_network: false`）。关键点：Hermes 自己新建容器时会在 `/root` 挂 tmpfs，把镜像里 `~/.bashrc` 的
  `conda activate testbed` 盖掉，还会加它自己的 cap-drop 和 512MB `/tmp` tmpfs。所以 runner 先按我们 harness
  的参数自己起容器，并打上 Hermes 持久容器复用所用的标签（`hermes-agent=1`、由每个实例唯一的
  `docker_shared_container_key` 推出的 `hermes-task-id`/`hermes-profile`、`hermes-egress=off`），Hermes 启动时
  会**附着**到这个容器（`_attach_existing_container`，它还会校验 NetworkMode 是 `none`）。每个实例的 key 都不同，
  所以并发时不会串到别人的容器。如果附着失败、Hermes 自己建了容器，runner 能通过额外标签发现，照样从那个容器取 patch，
  并在 `extra.deviation` 里记下来；`extra.attached_to_prepared_container` 记录是否附着成功。
  终端、文件工具、`execute_code` 都经这个 docker 环境执行（冒烟测试确认 python 是 testbed 环境、`curl` 解析不了 DNS）。
- **reasoning_content 回传**：Hermes 原生支持（`agent/reasoning_params.py` / `message_sanitization.py` 对
  DeepSeek 做回放，缺失时补 `" "`）。`--audit-requests` 打开 `HERMES_DUMP_REQUESTS`，runner 把每次请求的 dump
  汇总成 `logs/<id>.hermes.payload_audit.jsonl`（不含消息内容）然后删掉 dump：每次请求都是
  `thinking={type:enabled}`、`reasoning_effort=max`、`tool_choice` 不设，所有 assistant 消息都带 `reasoning_content`。
- **token/成本**：取自 `--usage-file`，主模型加辅助调用（标题生成、compaction 摘要等，走的也是同一个模型）一起算；
  `compactions` 是辅助调用里 compression 任务的次数。

## 与公平性条件的偏差

1. **任务文本末句**：“call finish.” → “stop.”（两个 agent 都没有 finish 工具）。三边其余文字逐字相同。
2. **Hermes 工具集**：默认的 `cli` 工具集去掉了 `web, browser, vision, image_gen, tts, computer_use, connections, cronjob`。
   这些工具在宿主上跑，而且能联网（浏览器/网页抓取能拿到上游修复），或者需要我们没提供的 key/显示器。保留的是
   `terminal, file, code_execution, todo, memory, skills, session_search, delegation, clarify`。
   记忆/会话搜索的作用域是每个实例全新的 `HERMES_HOME`，实例之间不会互相泄露。
3. **Hermes 步数上限**：`hermes -z` 里 `max_iterations` 是无上限（Hermes 的默认），runner 没法从外部按步数中止，
   只强制 3h 墙钟。`steps` 字段是它报告的 `api_calls`。
4. **pi 的 bash 每次都是新 shell**（pi 的本地实现本来就是这样），每条命令都会重新执行 conda 激活；我们的 harness
   用的是持久 shell。Hermes 的 docker 后端通过环境快照在命令之间保留 cwd 和环境变量。这是各 agent 自身的设计，没有改动。
5. **容器内运行时**：两个 agent 进程都在宿主上（LLM API 从宿主调用），容器是 `--network none`，
   没有“只放行 API 端点”的问题。Hermes 在启动时还会起两个临时容器，都不执行 agent 的命令：
   一个是 cgroup 探测 `docker run --rm <image> sleep 0`（默认网络，只跑 `sleep 0`）；另一个是系统提示词构建时的
   `prompt-backend-probe`（同一镜像、`--network none`，只跑 `uname`，跑完强制删除，并在 `HERMES_HOME/sandboxes`
   下留下 root 属主的目录，runner 会用一次性容器把它清掉）。
6. **超时参数**：沿用各自默认（pi bash 超时由模型自己传；Hermes 终端命令默认 180s，前台上限按 Hermes 默认）。
