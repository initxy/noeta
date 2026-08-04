# 用 Docker 部署

本指南教你把一个 Noeta host 打包成容器镜像并运行它 —— 单主机配 SQLite，或多主机配 Postgres。你需要一个驱动 `Client` 的 host 程序（见[你的第一个 agent](../tutorials/first-agent.md) 和 [`examples/reference-host`](https://github.com/initxy/noeta/tree/main/examples/reference-host)）。

Noeta 自己不提供 Dockerfile —— 库是进程内的，也没有守护进程 —— 所以下面给出的是一个 host 容器的规范形态。

## 1. 构建镜像

Noeta 是一个纯 Python 库。你的镜像装上 `noeta-sdk`、拷进你的 host 代码，然后运行它。运行时是进程内的 —— 没有 Noeta 守护进程要启动。

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System deps your tools need (git, curl, …). Trim to what your agent uses.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Noeta. Pin the version your host was built against.
RUN pip install --no-cache-dir "noeta-sdk>=0.4.0,<0.5.0"

# Copy your host.
COPY host.py .

# The workspace the agent operates on. Mount a volume here for persistence.
RUN mkdir -p /workspace
VOLUME ["/workspace"]

# Noeta reads provider / model keys from the environment.
ENV NOETA_MODEL="claude-sonnet-4-5-20250929"

CMD ["python", "host.py"]
```

构建并运行：

```bash
docker build -t my-noeta-host .
docker run --rm -it \
    -v "$(pwd)/workspace:/workspace" \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    my-noeta-host
```

```
Successfully tagged my-noeta-host:latest
```

## 2. 单主机：SQLite

对于一个容器（或一个 host 进程），SQLite 是默认的持久化存储。把 `HostConfig.storage_path` 指向挂载卷上的一个文件：

```python
from noeta.sdk import Client, HostConfig, Options

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=my_provider,
    workspace_dir="/workspace",
    model="claude-sonnet-4-5-20250929",
    host_config=HostConfig(storage_path="/data/noeta.sqlite"),
)
```

挂上数据卷，让存储在容器重启后依然存在：

```bash
docker run -v noeta-data:/data -v "$(pwd)/workspace:/workspace" my-noeta-host
```

SQLite **只支持单主机** —— 它没有跨进程的写入围栏。要让多个容器共享一个存储，请用 Postgres。

## 3. 多主机：Postgres

Postgres 让你在多个 host 进程之间、甚至跨机器地获得带 lease 围栏的写入。把 `storage_path` 指向一个 `postgresql://` DSN：

```python
host_config = HostConfig(
    storage_path="postgresql://noeta:noeta@postgres:5432/noeta",
)
```

一份让 host 池跑在 Postgres 上的 `docker-compose.yml`：

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
      NOETA_MODEL: claude-sonnet-4-5-20250929
      NOETA_STORAGE_PATH: postgresql://noeta:noeta@postgres:5432/noeta
    volumes:
      - ./workspace:/workspace
    # Scale out: docker compose up --scale worker=4
    deploy:
      replicas: 1

volumes:
  pgdata:
```

横向扩展 worker 池：

```bash
docker compose up --scale worker=4
```

```
 ✔ Container noeta-postgres-1  Healthy
 ✔ Container noeta-worker-1    Started
 ✔ Container noeta-worker-2    Started
 ✔ Container noeta-worker-3    Started
 ✔ Container noeta-worker-4    Started
```

每个 worker 都排空同一个 Postgres 支撑的 Dispatcher。lease 围栏（对着数据库时钟做的 `FOR SHARE` 行检查）保证一个 lease 已被回收的 Task 不可能绕到新一代身后写入。

## 4. 可选：在容器里跑一个 sandbox

如果你的 host 要为每个会话置备容器（见[使用 Sandbox](use-sandbox.md)），那么 host 容器需要能访问容器运行时。两种常见形态：

### Docker-outside-of-Docker（DooD）

挂载宿主机的 Docker socket，让 host 可以派生兄弟容器：

```bash
docker run -v /var/run/docker.sock:/var/run/docker.sock my-noeta-host
```

sandbox 那篇指南里的 `DockerSandboxProvider` 无需改动即可工作 —— 它 shell out 到 `docker`，而 `docker` 现在对话的是宿主机的守护进程。

### Sidecar sandbox

把 sandbox 容器作为 sidecar 跑在同一个 pod / compose 栈里，并通过 `SandboxExecEnvConfig` 接上去：

```python
from noeta.sdk import HostConfig, SandboxExecEnvConfig

host_config = HostConfig(
    exec_env=SandboxExecEnvConfig(
        base_url="http://sandbox:8080",
        api_key_env="SANDBOX_API_KEY",
        workdir="/workspace",
    ),
)
```

```yaml
services:
  sandbox:
    image: aio-sandbox:latest
    environment:
      SANDBOX_API_KEY: ${SANDBOX_API_KEY}
    volumes:
      - ./workspace:/workspace

  host:
    build: .
    depends_on: [sandbox]
    environment:
      SANDBOX_API_KEY: ${SANDBOX_API_KEY}
    volumes:
      - ./workspace:/workspace
```

## 环境变量

Noeta 从环境读取 provider 和 sandbox 的凭证 —— 永远不要硬编码它们。

| 变量 | 用途 |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic provider |
| `OPENAI_API_KEY` | OpenAI 兼容 provider |
| `NOETA_WEB_SEARCH_API_KEY` | `WebSearch` 工具 |
| `SANDBOX_API_KEY` | sandbox 容器认证（连接时读取） |

## 下一步

- [部署 Worker](deploy-worker.md) —— 常驻 worker 池及其各项参数
- [使用 Sandbox](use-sandbox.md) —— `ExecEnv` / `SandboxProvider` 接缝
- [已知限制](../operations/limitations.md) —— SQLite 的单主机边界
- [故障排查](../operations/troubleshooting.md) —— 生产中的关闭与 lease 现象
