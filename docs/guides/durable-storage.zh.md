# 持久化存储 { #durable-storage }

默认情况下，Noeta 使用内存存储运行——对话和会话列表在进程退出时消失。要在重启后持久化会话，请将后端指向一个存储 URL：SQLite 文件路径或 PostgreSQL DSN。

## 启用持久化 { #enabling-persistence }

### 通过环境变量 { #via-env-var }

```bash
# SQLite 文件
NOETA_AGENT_STORAGE=./sessions.sqlite python -m noeta.agent

# PostgreSQL（需要 `pip install noeta-runtime[postgres]`）
NOETA_AGENT_STORAGE=postgresql://user:pass@localhost:5432/noeta python -m noeta.agent
```

旧写法 `NOETA_AGENT_SQLITE` 对文件路径仍然有效。

### 通过配置文件 { #via-config-file }

将 `storage_url` 添加到你的 `NOETA_AGENT_CONFIG` JSON（旧键 `sqlite_path` 仍被接受）：

```json
{
  "provider_id": "openai",
  "model": "gpt-5.5",
  "base_url": "https://api.openai.com/v1",
  "api_key": "<your-key>",
  "workspace_dir": ".",
  "storage_url": "./sessions.sqlite"
}
```

```bash
NOETA_AGENT_CONFIG=noeta.config.json python -m noeta.agent
```

### 进程内（SDK） { #in-process-sdk }

在你自己的应用中嵌入引擎时，自己构建存储三元组并通过 `HostConfig` 传递：

```python
from pathlib import Path
from noeta.agent.host.storage import open_durable_storage
from noeta.sdk import Client, HostConfig, Options

options = Options(
    system_prompt="You are a helpful assistant.",
    name="main",
    allowed_tools=("read", "write"),
    permission_mode="bypassPermissions",
)

# sqlite 路径或 postgresql:// DSN——同一个调用。
(event_log, content_store, dispatcher), storage_close = open_durable_storage(
    "./sessions.sqlite"
)

host_config = HostConfig(
    event_log=event_log,
    content_store=content_store,
    dispatcher=dispatcher,
)

client = Client(
    options,
    provider=my_provider,
    workspace_dir=Path("./my-project"),
    model="gpt-5.5",
    host_config=host_config,
)

try:
    outcome = client.start(goal="Analyze this codebase.")
    # ... sessions survive across restarts
finally:
    client.shutdown()
    storage_close()
```

## 存储内容 { #what-gets-stored }

一个 SQLite 文件——或一个 PostgreSQL 数据库——支持所有三个存储 adapter（`Sqlite*` / `Postgres*` 前缀，行为契约相同）：

| Adapter | 存储内容 |
| --- | --- |
| `SqliteEventLog` / `PostgresEventLog` | 每任务的 `EventEnvelope` 记录——每一步的完整历史（消息、工具调用、决策、状态变化）。 |
| `SqliteContentStore` / `PostgresContentStore` | 大于 4 KB 事件载荷上限的内容寻址 blob：完整的 LLM 请求/响应主体、大型工具输出、上传的图片。 |
| `SqliteDispatcher` / `PostgresDispatcher` | Worker 租约状态 + 唤醒事件队列。让重启的进程回收过期租约并交付待处理的唤醒事件。 |

SQLite 文件在首次写入时自动创建；PostgreSQL schema 在首次连接时自动创建（DSN 指向的数据库必须已存在）。使用 `:memory:` 获取内存中的 SQLite 实例——对测试有用。

## 选择后端 { #choosing-a-backend }

- **SQLite** 是单机零配置的默认选择：一个文件、无服务器、完整持久性（`synchronous=FULL`、WAL）。
- **PostgreSQL** 把同样的三个 adapter 放到数据库服务器上——当存储需要离开本机（托管数据库、备份、运维工具）时选它。安装 extra：`pip install noeta-runtime[postgres]`。

## Fold 恢复如何工作 { #how-fold-recovery-works }

当后端以设置了 `storage_url` 启动时，它：

1. 在同一文件/数据库上打开三个 adapters。
2. `GET /tasks` 只读视图将每个任务的 envelope 流 fold 为 status/title/closed 摘要——侧边栏会话列表立即可见。
3. 当你点击一个会话时，前端打开 `GET /stream?task=<id>` 并从 seq 0 重放 envelopes，重建完整的对话视图。

这是 **fold，而非 load**：Engine 从不反序列化"状态"对象。它通过在实时执行期间使用的相同 fold 函数重放 envelopes，重新推导出每个状态切片（RuntimeState、TaskState、ContextState、GovernanceState）。

## 要点 { #key-points }

- **`NOETA_AGENT_STORAGE`** 是环境变量；`storage_url` 是配置文件键（旧写法 `NOETA_AGENT_SQLITE` / `sqlite_path` 仍被接受）。SDK 默认值是内存（无文件）。
- **一个数据库，三个 adapters。** `open_durable_storage()` 一起构建所有三个，因此 event log 已经将 dispatcher 作为其 `lease_validator` 持有。
- **Fold 是确定性的。** 重放相同的 envelopes 总是产生相同的状态——没有单独的"状态表"可能漂移。
- **`storage_close()`** 是你在进程内使用 SDK 时的责任。应用后端在关闭时自动处理。

## 来源 { #source }

- `apps/noeta-agent/noeta/agent/host/storage.py` —— `open_durable_storage()`
- `apps/noeta-agent/noeta/agent/backend/lifecycle.py` —— `BackendConfig.from_env()`（`NOETA_AGENT_STORAGE` → `storage_url`）
- SQLite adapters：`packages/noeta-runtime/noeta/storage/sqlite/`
- PostgreSQL adapters：`packages/noeta-runtime/noeta/storage/postgres/`
- `HostConfig`：`packages/noeta-sdk/noeta/client/host_config.py`
- 另见：[核心概念](../concepts.md#eventlog)、[配置](../reference/configuration.md)、[ADR：事件溯源真相](../adr/event-sourced-truth.md)、[ADR：存储协议 L0](../adr/storage-protocols-l0.md)
