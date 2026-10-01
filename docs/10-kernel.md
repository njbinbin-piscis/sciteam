# 10 · 内核：EventStore、BlobStore、ConfigStore

> 所属层：1。所有上层状态都由这里的事件折叠得到。

## 1. EventStore

### 1.1 职责

- 追加写入不可变事件；每个流一条哈希链，全局一个单调序号。
- 按流读取、按过滤条件订阅（异步迭代），供 harness 恢复、调度器唤醒、Web 实时推送使用。
- 校验哈希链。

不提供更新或删除。「遗忘」（如压缩）本身也是一条事件。

### 1.2 数据模型

```python
class Event(BaseModel):
    seq: int                 # 全局单调，由存储分配
    stream: str              # 如 "run/<run_id>", "agent/<agent_id>/act/<activation_id>"
    stream_seq: int          # 流内从 1 开始
    type: str                # 事件类型，见各模块的事件清单
    actor: str | None        # 产生者：agent id、"system"、"human:<id>"
    payload: dict            # 由各模块的 pydantic 模型 model_dump(mode="json") 得到
    causation_id: int | None # 由哪条事件引起（全局 seq）
    rule_version: str | None # 第三层起填写
    ts: datetime             # UTC
    prev_hash: str           # 同流上一条的 hash；流首为 64 个 "0"
    hash: str                # sha256(canonical_json(除 seq、hash 外的全部字段))
```

`canonical_json`：`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`；datetime 用 ISO-8601 UTC 带 `Z`。`seq` 不进哈希，这样同一流可以在不同库之间迁移而哈希不变。

### 1.3 存储

SQLite，WAL 模式，单文件 `runs/<run_id>/events.db`（第一层每次运行一个库；第二层起一个组织一个库）。

```sql
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  stream TEXT NOT NULL,
  stream_seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  actor TEXT,
  payload TEXT NOT NULL,
  causation_id INTEGER,
  rule_version TEXT,
  ts TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  UNIQUE (stream, stream_seq)
);
CREATE INDEX events_type ON events(type);
```

### 1.4 接口

```python
class EventStore:
    def __init__(self, path: Path): ...
    def append(self, stream: str, type: str, payload: BaseModel | dict, *,
               actor: str | None = None, causation_id: int | None = None,
               rule_version: str | None = None,
               expected_stream_seq: int | None = None) -> Event: ...
    def read(self, stream: str, *, after_stream_seq: int = 0) -> list[Event]: ...
    def read_global(self, *, after_seq: int = 0, types: set[str] | None = None,
                    stream_prefix: str | None = None, limit: int | None = None) -> list[Event]: ...
    async def subscribe(self, *, after_seq: int = 0, types: set[str] | None = None,
                        stream_prefix: str | None = None) -> AsyncIterator[Event]: ...
    def verify(self, stream: str) -> VerifyReport: ...
    def streams(self, prefix: str = "") -> list[str]: ...
```

- `append` 在一个事务内读取流尾、计算哈希、写入。`expected_stream_seq` 不为空且与流尾不符时抛 `ConcurrencyConflict`，调用方重读后重试（乐观并发）。
- 进程内单写者：所有写入经过同一个 `EventStore` 实例上的 `threading.Lock`；多进程写同一库不在第一层范围。
- `subscribe` 实现：进程内写入后通知条件变量；另以 200ms 轮询兜底，保证跨进程读者（Web）也能看到新事件。

### 1.5 折叠

```python
def fold(events: Iterable[Event], reducer: Callable[[S, Event], S], initial: S) -> S
```

没有缓存的可变状态。需要性能时由上层维护「投影缓存」，缓存以 `(stream, 已折叠到的 stream_seq)` 为键，可随时丢弃重建。

### 1.6 错误语义

- `ConcurrencyConflict`：乐观并发冲突。
- `IntegrityError`：`verify` 发现哈希不连续或内容被改。
- payload 不可 JSON 序列化：在 `append` 时立即 `TypeError`，不写入。

## 2. BlobStore

### 2.1 职责

大内容按内容寻址存储：工具完整输出、压缩前的原文、产物文件、供应商原始响应（可选）。事件里只放引用。

### 2.2 接口

```python
class BlobRef(BaseModel):
    sha256: str
    size: int
    media_type: str = "text/plain"

class BlobStore:
    def __init__(self, root: Path): ...
    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> BlobRef: ...
    def put_text(self, text: str) -> BlobRef: ...
    def get_bytes(self, ref: BlobRef | str) -> bytes: ...
    def get_text(self, ref: BlobRef | str) -> str: ...
    def read_slice(self, ref: BlobRef | str, *, offset: int, limit: int) -> str: ...  # 按行
    def exists(self, sha256: str) -> bool: ...
```

存储布局：`<root>/<sha[0:2]>/<sha>`。写入先写临时文件再原子改名，重复写同一内容是空操作。

## 3. ConfigStore

### 3.1 职责

- 读取 `configs/` 下的配置文件（YAML、Markdown），按 pydantic schema 校验。
- 对一次运行实际读到的全部文件计算内容摘要，生成 `ConfigSnapshot`，写入 `run_started`。
- 支持覆盖层：实验的 arm 是对 `configs/` 的覆盖目录，同名文件覆盖基线。

### 3.2 接口

```python
class ConfigSnapshot(BaseModel):
    files: dict[str, str]        # 相对路径 -> sha256
    overlays: list[str]          # 覆盖层目录，按优先级从低到高
    interpreter_version: str     # sciteam 包版本 + git 提交（若可得）
    digest: str                  # 对 files 的规范化摘要

class ConfigStore:
    def __init__(self, base: Path, overlays: Sequence[Path] = ()): ...
    def load(self, rel_path: str, schema: type[M]) -> M: ...
    def load_text(self, rel_path: str) -> str: ...
    def snapshot(self) -> ConfigSnapshot: ...   # 只包含本次已 load 过的文件
```

第三层修宪产生新版本时，由 `institution/amendment.py` 写入新文件并生成新的 `ConfigSnapshot`；ConfigStore 本身只读。

## 4. ID 与时钟

- ID：`<prefix>_<ulid>`，如 `run_01J…`、`act_01J…`。ULID 保证按时间可排序。
- 时钟：`Clock` Protocol（`now()`、`monotonic()`），测试注入假时钟。任何模块不直接调用 `datetime.now()`。

## 5. 测试清单

- 单元：
  - 追加与读取往返一致；
  - 流内 `stream_seq` 连续；
  - 篡改 payload 后 `verify` 报错；
  - 乐观并发冲突抛异常；
  - `canonical_json` 对键顺序不敏感；
  - 订阅能收到订阅之后写入的事件，且不重复；
  - BlobStore 重复写入幂等，`read_slice` 边界正确；
  - ConfigStore 覆盖层优先级正确，快照只包含已读文件，摘要稳定。
- 集成：两个线程并发追加 10,000 条事件到不同流与同一流，全局 `seq` 严格递增，所有流 `verify` 通过。
- 恢复：写入过程中杀进程，重开后库可读、`verify` 通过（依赖 SQLite 事务原子性）。
