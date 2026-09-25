# 部署上线

同一个 agent，从单个进程一路扩到多台机器共用 Postgres。变的只是存储和 worker 池，agent 的定义不用动。

<NtScale lang="zh" />

| 阶段 | 存储 | 谁来推进任务 | 崩溃后能恢复吗 |
| --- | --- | --- | --- |
| 进程内 | 内存（默认） | 调用方线程 | 不能 |
| 单机 | SQLite 文件 | `Client.start_workers(n)` | 能 |
| 多机 | Postgres | 每台机器各跑一个 worker 池 | 能，而且机器之间可以互相接手任务 |

## 1. 进程内

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import AnthropicProvider

client = Client(Options(system_prompt="You are a helpful assistant."), provider=AnthropicProvider(), model="claude-sonnet-5")
outcome = client.start(goal="Summarize README.md.")
```

不传 `HostConfig` 时，事件日志放在内存里，进程退出就没了。写脚本、跑测试够用；定时器不会触发，出了事也恢复不了。

## 2. 单机：SQLite + worker 池

```python
from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    workspace_dir="./workspace",
    host_config=HostConfig(storage_path="./noeta.sqlite"),
)

with client:
    client.start_workers(4)
    seeded = client.seed_start(goal="Summarize README.md.")
    client.dispatch_seeded(seeded)      # returns at once; a worker drives the turn
    ...
```

- `storage_path` 可以是 SQLite 文件路径、`postgresql://` 连接串，或 `":memory:"`。
- `start_workers(n)` 起 `n` 个守护线程，从就绪队列里取任务来跑。没有 worker 池的话，`wait_timer` 永远等不到时间，worker 崩掉的任务也没人接手。
- HTTP 接口里用 `seed_start` + `dispatch_seeded`：返回之前这一轮已经落盘，之后由 worker 去跑。`start()` 仍然在调用方线程上把这一轮跑完。
- `start_workers` 只能调一次，第二次会抛 `RuntimeError`。

## 3. 多机：Postgres

```python
host_config = HostConfig(storage_path="postgresql://noeta:noeta@postgres:5432/noeta")
```

每台机器跑自己的 worker 池，连同一个数据库。每次写入都在数据库事务里核对当前租约（lease），租约已被别人接走的 worker 写不进去。Postgres 驱动随 `noeta-sdk` 一起安装。

::: warning SQLite 只能单机
SQLite 没有跨机器的租约检查。同一个进程里跑多个 worker 没问题；多个进程或多台机器共用一个 SQLite 文件不行。
:::

**同一个数据库，跑不同的 agent。** 给每个 client 设各自的 `HostConfig(queue="…")`。根任务建在创建它的 client 的队列上，子任务跟着父任务走，worker 池只认自己的队列，所以配置不同的两个 client 不会跑到对方的任务。见 [ADR：worker queue routing](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md)。

## 用 Docker 打包

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# ripgrep is required: the Grep and Glob tools run `rg`, and fail without it.
# git and the rest are whatever your own tools need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "noeta-sdk>=0.6,<0.7"

COPY host.py .
RUN mkdir -p /workspace
VOLUME ["/workspace"]

CMD ["python", "host.py"]
```

::: tip ripgrep 必须装
`Grep` 和 `Glob` 工具靠 `rg` 干活，镜像里没有 ripgrep，agent 第一次搜索就会失败。
:::

`host.py` 一直跑 worker 池，直到容器被停掉：

```python
import os
import signal
import threading

from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    workspace_dir="/workspace",
    host_config=HostConfig(storage_path=os.environ["NOETA_STORAGE_PATH"]),
)

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())

with client:                      # leaving the block stops the pool
    client.start_workers(4)
    stop.wait()
```

多个 worker 共用 Postgres 的 `docker-compose.yml`：

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: noeta
      POSTGRES_PASSWORD: noeta
      POSTGRES_DB: noeta
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U noeta"]
      interval: 5s
      timeout: 5s
      retries: 10

  worker:
    build: .
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      NOETA_STORAGE_PATH: postgresql://noeta:noeta@postgres:5432/noeta
    volumes:
      - ./workspace:/workspace

volumes:
  pgdata:
```

```bash
docker compose up --scale worker=4
```

单个容器用 SQLite 的话，把 `storage_path` 指向挂载卷上的文件（`-v noeta-data:/data`，`storage_path="/data/noeta.sqlite"`）。

| 环境变量 | 谁读 |
| --- | --- |
| `ANTHROPIC_API_KEY` | `AnthropicProvider()` |
| `OPENAI_API_KEY` | OpenAI 兼容的 provider |
| `NOETA_WEB_SEARCH_API_KEY` | 设了才有 `WebSearch` 工具 |
| `SANDBOX_API_KEY` | 沙箱容器鉴权（见[沙箱](sandbox.md)） |

上面的 `NOETA_STORAGE_PATH` 是这个例子自己约定的，由 `host.py` 读取，Noeta 本身不读它。

## 调 worker 池

下面都是 `start_workers` 的关键字参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `num_workers` | `1` | worker 线程数（至少 1） |
| `poll_interval` | `0.1` 秒 | 就绪队列为空时睡多久 |
| `heartbeat_interval` | `30.0` 秒 | 正在跑的一步多久续一次租约 |
| `stale_sweep_interval` | `10.0` 秒 | 多久回收一次过期租约 |
| `timer_poll_interval` | `1.0` 秒 | 多久检查一次到期的定时器 |
| `lease_seconds` | `600.0` 秒 | 每个任务的初始租约时长 |
| `shutdown_grace_s` | `10.0` 秒 | 停机时等当前这一步跑完的最长时间；`None` 表示一直等 |

## 停机

```python
stopped = client.stop_workers(timeout=30.0)   # True if every worker exited
```

worker 不再接新任务，最多等 `shutdown_grace_s` 让手上这一步跑完。超时的那一步会被放弃，但它可能还在写日志，所以之后**必须退出进程**。进程退出后租约过期，别的 worker 会把任务接过去。`stop_workers` 超时会返回 `False`，但仍记着这个池，可以再调一次。`Client.shutdown()`（以及离开 `with client:` 块）会先停 worker 池。

不用 `Client` 的 host 可以直接跑这个循环，见 [WorkerLoop](../reference/worker-loop.md)。

## 下一步

- [沙箱](sandbox.md)：让 agent 的工具在容器里跑
- [任务与唤醒](../how-it-works/tasks-and-waking.md)：worker 接到任务后做什么
- [已知限制](../operations/limitations.md)：SQLite 和崩溃恢复的边界
