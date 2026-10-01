# 11 · Providers：LLM、沙箱、文献

> 所属层：1。所有外部 IO 只经这里的 Protocol；实现由组合根注入，测试注入假实现。

## 1. LLM

### 1.1 Protocol

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict            # JSON Schema

class ToolCall(BaseModel):
    id: str
    name: str
    arguments_raw: str          # 原样保留，解析失败时交给 harness 处理
    arguments: dict | None      # 解析成功时填写

class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False     # 供应商未返回 usage 时为 True

class AssistantTurn(BaseModel):
    content: str
    reasoning: str | None       # DeepSeek 的 reasoning_content
    tool_calls: list[ToolCall]
    finish_reason: str          # stop | tool_calls | length | content_filter | error
    usage: Usage
    model: str                  # 供应商回报的实际模型名
    raw_ref: BlobRef | None     # 原始响应（可选存档）

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    reasoning: str | None = None
    tool_calls: list[ToolCall] = []
    tool_call_id: str | None = None

class LlmProvider(Protocol):
    model_info: ModelInfo
    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec], *,
                       params: CallParams) -> AssistantTurn: ...

class CallParams(BaseModel):
    reasoning_effort: str | None     # 取模型目录里的最高档
    max_output_tokens: int | None
    tool_choice: Literal["auto", "required", "none"] | dict = "auto"
    timeout_s: float
    seed: int | None = None
```

### 1.2 模型目录（`configs/models.yaml`）

每个模型一条，所有与模型相关的数字只在这里出现：

```yaml
deepseek-flash:
  provider: openai_compat
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  wire_model: deepseek-flash
  context_window: 1000000
  max_output_tokens: 384000
  reasoning: {supported: true, efforts: [low, high, max], default_effort: max,
              passback_with_tools: true}
  pricing_usd_per_mtok:   # 用于费用断路器与报告
    peak:     {cache_hit: 0.006, cache_miss: 0.30, output: 1.20}
    off_peak: {cache_hit: 0.003, cache_miss: 0.15, output: 0.60}
  peak_hours_utc: ["01:00-04:00", "06:00-10:00"]   # 周一至周五
  concurrency_limit: 2500
deepseek-v4-pro:
  # 同上，价格与并发按官方页面
```

`passback_with_tools: true` 表示：请求带 `tools` 时，所有历史 assistant 消息的推理必须原样回传，否则供应商返回 400。harness 的上下文引擎据此保留推理（`20-harness.md` §4）。

### 1.3 OpenAI 兼容适配（`providers/llm_openai_compat.py`）

- 传输：httpx 异步客户端，**流式**请求（避免长推理时的读超时），在适配器内部拼装为完整的 `AssistantTurn`。
- DeepSeek 专有字段：`reasoning_effort`、`extra_body={"thinking": {"type": "enabled"}}`；assistant 消息回传时带 `reasoning_content`。
- 工具调用参数按流片段拼接；最终 `json.loads` 失败时 `arguments=None`，`arguments_raw` 保留原文。
- usage 字段映射：`prompt_cache_hit_tokens` → `cache_hit_tokens`，`prompt_cache_miss_tokens` → `cache_miss_tokens`，`completion_tokens_details.reasoning_tokens` → `reasoning_tokens`。
- 错误分类（供 `20-harness.md` §9 恢复策略使用）：

| 类别 | 判定 | harness 处理 |
|---|---|---|
| `RateLimited` | 429 | 按 `Retry-After` 或指数退避重试 |
| `Transient` | 5xx、连接中断、流中断、超时 | 指数退避重试 |
| `ContextOverflow` | 400 且报文指明超长 | 强制压缩后重试 |
| `BadRequest` | 其他 400 | 记录后以 `error` 结局结束，不重试 |
| `Auth` | 401/403 | 立即以 `error` 结束 |
| `QuotaExhausted`（`provider_quota`） | 402，或报文含 "insufficient balance" / "insufficient_quota" 等（优先于其他判定） | 立即以 `error(reason=provider_quota)` 结束，不重试；基准驱动据此熔断整批运行，并在续跑时重跑该题 |

2026-09-24 在正式运行中遇到 DeepSeek 余额耗尽（402 Insufficient Balance）。当时尚无该类别，402 被归为 `BadRequest`，导致 34 分钟内 243 题被判为 error，pi 与 Hermes 也同样秒失败了整份题单。这些题现已追加重分类记录并重跑。

### 1.4 费用计算

`cost_usd(usage, model_info, ts)`：按调用时刻落在高峰或低峰选价；`cache_hit × 命中价 + cache_miss × 未命中价 + completion × 输出价`。若 `cache_miss_tokens` 缺失，用 `prompt_tokens - cache_hit_tokens`。

### 1.5 假实现

- `ScriptedLlm`：按脚本逐轮返回预设的 `AssistantTurn`，并返回非空 usage（避免旧系统那种「假客户端从不返回 usage，所以计量代码从未被执行」的问题）。
- `RecordingLlm`：包装真实供应商，把每次请求与响应存入 BlobStore，供离线回放测试使用。

## 2. 沙箱

### 2.1 Protocol

```python
class ExecResult(BaseModel):
    exit_code: int | None       # 超时为 None
    output: str                 # stdout 与 stderr 合并，按时间顺序
    timed_out: bool
    duration_s: float

class Sandbox(Protocol):
    async def start(self) -> None: ...
    async def run(self, command: str, *, timeout_s: float) -> ExecResult: ...   # 持久 shell
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def close(self) -> None: ...
    @property
    def workdir(self) -> str: ...
```

### 2.2 Docker 实现（`providers/sandbox_docker.py`）

- 每次激活一个容器：`docker run -d --network <policy> --memory <m> --cpus <c> <image> sleep infinity`。镜像不存在时先 `docker pull`，拉取超时与命令超时分开配置（旧系统踩过的坑）。
- **持久 shell**：容器内启动一个长驻 `bash`（经 `docker exec -i`），每条命令后追加哨兵 `echo __SCITEAM_DONE_<nonce>__ $?`，按哨兵切分输出并取退出码。这样 `cd`、环境变量、激活的 conda 环境在命令之间保留。
- 超时：到时向进程组发 `SIGINT`，2 秒后 `SIGKILL`，并重启 shell；结果标 `timed_out`，并告知模型 shell 已重启、工作目录已恢复为默认。
- 文件读写走 `docker cp` 或 `exec cat`/`tee`，二进制安全。
- 清理：`close()` 在 `finally` 中执行；`start()` 中途失败也要先 `close()` 再抛出（旧系统的容器泄漏缺陷）。进程退出钩子清理本进程创建的全部容器，容器带 `sciteam.run=<run_id>` 标签。
- 网络策略由 harness 配置给出：基准任务默认 `none`，防止从上游仓库直接取修复。

### 2.3 本地实现

`LocalSandbox`：在临时目录下运行，仅用于单元测试和不需要隔离的开发任务。

## 3. 文献

```python
class Paper(BaseModel):
    id: str                  # doi:… | arxiv:… | openalex:…
    title: str
    authors: list[str]
    year: int | None
    venue: str | None
    abstract: str | None
    url: str | None

class LiteratureProvider(Protocol):
    async def search(self, query: str, *, limit: int, year_from: int | None = None) -> list[Paper]: ...
    async def resolve(self, identifier: str) -> Paper | None: ...     # DOI / arXiv id / 标题
```

- 实现：OpenAlex（检索）、Crossref（DOI 解析）、arXiv API（预印本）。
- 缓存：以 `(provider, method, 规范化参数)` 的哈希为键，把响应存入 BlobStore，并写一条 `literature_cached` 事件。离线重放时只读缓存，不联网。
- 第一层只用于功能检查（文献综述任务）；引用可解析验证器在第三层使用同一 Provider。

## 4. 测试清单

- LLM 适配器：
  - 用录制的真实流式响应（含推理、多个工具调用、参数分片）回放，拼装结果与原始响应一致；
  - 错误分类覆盖上表每一类；
  - 费用计算覆盖高峰与低峰、缺失 `cache_miss` 字段两种情况。
- 真实冒烟（需密钥，CI 默认跳过）：`deepseek-flash` 与 `deepseek-v4-pro` 各一次带工具的两轮对话，验证推理回传不报 400、usage 字段齐全。
- Docker 沙箱：
  - `cd` 与环境变量在命令间保留；
  - 超时后 shell 自动重启且可继续使用；
  - 二进制文件读写往返一致；
  - `start` 失败不遗留容器；
  - 100 条命令连续执行无输出串扰。
- 文献：缓存命中时不发网络请求；离线模式下未命中缓存要报错，不能静默返回空。
