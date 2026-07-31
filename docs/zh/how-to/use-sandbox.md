# 使用 Sandbox

本指南教你把一个 agent 的文件、shell 和浏览器副作用路由进容器，让不受信任的 agent 永远不直接碰到宿主机。你需要能用 `Client` 驱动一个 agent（[你的第一个 agent](../tutorials/first-agent.md)），以及一个容器运行时。

Noeta 提供这条接缝（`ExecEnv`、`SandboxProvider`）和容器线的适配器。**容器的置备是 host 的活** —— 库不会替你跑任何 `docker`。

## 挑一种模式

`HostConfig` 上有两个入口，取决于你想要一个共享容器还是每个会话一个新容器：

| 模式 | `HostConfig` 字段 | 什么时候用 |
| --- | --- | --- |
| **Attach**（一个共享容器） | `exec_env: SandboxExecEnvConfig` | 一个长期存活的容器，每个会话都接上去 —— 最简单，适合开发。 |
| **Provision**（每会话一个容器） | `sandbox_provider: SandboxProvider` + `sandbox_spec: SandboxSpec` | 会话打开时分配一个新容器，会话结束时拆掉 —— 生产级隔离。 |

两者都设了时，`SandboxProvider` 优先。

## 模式 A —— 接上一个运行中的容器

最快的路径：自己先起一个容器（例如一个 [AIO Sandbox](https://github.com/bytedance/aio-sandbox) 镜像），然后把它的地址告诉 Noeta。

```python
from noeta.sdk import Client, HostConfig, Options, SandboxExecEnvConfig

host_config = HostConfig(
    exec_env=SandboxExecEnvConfig(
        base_url="http://localhost:8080",
        api_key_env="SANDBOX_API_KEY",   # read from env at connect time
        workdir="/workspace",
    ),
)

client = Client(
    Options(system_prompt="You are a coding agent."),
    provider=my_provider,
    workspace_dir="./workspace",
    model="claude-sonnet-4-5-20250929",
    host_config=host_config,
)
```

API key **只在发起请求时**从 `api_key_env` 指名的环境变量里读取 —— 它永远不会进入配置、日志或事件。在第一轮之前设好它：

```bash
export SANDBOX_API_KEY=your-container-key
```

让 agent 跑点能暴露自己所在位置的东西，来验证这条路由：

```
shell_run(command="hostname")  →  a1b2c3d4e5f6   # the container, not your host
```

在 attach 模式下，每个会话共用同一个容器。`release` 是空操作，因为 SDK 并不拥有该容器的生命周期。

## 模式 B —— 每个会话置备一个

要做到按会话隔离，实现三方法的 `SandboxProvider` 协议，并把它作为 `HostConfig.sandbox_provider` 传入。SDK 会在根 Task 打开时调 `allocate`，在恢复 / 过期回收时调 `attach`，在根 Task 到达终态时调 `release`。

```python
from noeta.sdk import (
    SandboxSpec, SandboxHandle, StaticApiKeyAuth, decode_exec_env_ref,
)

class DockerSandboxProvider:
    """Provision a fresh container per root-task tree via the Docker CLI."""

    def allocate(self, root_task_id: str, spec: SandboxSpec) -> SandboxHandle:
        # 1. Run the container. Mount the workspace; spec.mounts carries the
        #    assembled list (workspace + skills + host extensions).
        mounts = " ".join(
            f"-v {m.source}:{m.target}:{m.mode}" for m in spec.mounts
        )
        import subprocess
        result = subprocess.run(
            ["docker", "run", "-d", "--rm",
             *mounts.split(),
             spec.image,
             "sleep", "infinity"],
            check=True, capture_output=True, text=True,
        )
        container_id = result.stdout.strip()

        # 2. Probe readiness, then return the handle. base_url must be the
        #    container's API root; workdir is the container-side workspace.
        return SandboxHandle(
            base_url="http://localhost:8080",
            sandbox_id=container_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        # Reconnect to a container recorded on TaskHostBound.exec_env_ref.
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def release(self, root_task_id: str) -> None:
        # Idempotent: look up the container for this root task and stop it.
        # A real provider keeps a root_task_id -> container_id map.
        ...
```

接上它：

```python
from noeta.sdk import HostConfig, SandboxSpec, MountSpec

host_config = HostConfig(
    sandbox_provider=DockerSandboxProvider(),
    sandbox_spec=SandboxSpec(
        image="aio-sandbox:latest",
        mounts=(
            MountSpec(source="./workspace", target="/workspace", mode="rw"),
        ),
        resources={"memory": "2g", "cpus": "2"},
    ),
)
```

### `SandboxSpec` 携带什么

| 字段 | 形状 | 作用 |
| --- | --- | --- |
| `image` | `str` | 要运行的容器镜像。 |
| `mounts` | `tuple[MountSpec, ...]` | 工作区 + skills + host 挂载。`MountSpec.kind` 取 `"local-path"` / `"volume"` / `"nas"` / `"pvc"`，好让分布式 provider 把同一套形状映射到它自己的存储后端。 |
| `resources` | `Mapping[str, str]` | 内存 / CPU 上限 —— 原样传给你的置备器。 |
| `env` | `Mapping[str, str]` | 额外的容器环境变量。 |

### 持久绑定：`exec_env_ref`

一个会话的容器地址会以 `"{base_url}#{sandbox_id}"` 这个扁平字符串记录在 `TaskHostBound.exec_env_ref` 上。恢复或过期回收时，SDK 调用 `provider.attach(exec_env_ref)` —— 而不是 `allocate` —— 因此被恢复的 Task 重新接回**同一个**容器。凭证**不在**这个 ref 里；重新接入的 host 从它自己的环境重建 `SandboxAuth`。

## 什么跑在容器里

在 sandbox 下，这些副作用会经由会话的 `ExecEnv` 路由：

- **fs 工具** —— `read`、`write`、`edit`、`glob`、`grep`、`apply_patch`
- **前台 shell** —— `shell_run`（后台 shell 在 host 侧，在容器下会被拒绝）
- **网络出口** —— `webfetch` / `web_search` 经由容器内的 `curl` 出去
- **skill 索引** —— `tree_snapshot` 把整次遍历打包成一次往返
- **浏览器工具** —— `browser_navigate`、`browser_click`、…… 经由 `BrowserBackend` 线

刻意留在 **host 侧**的：`memory_*`（全局的跨会话存储）、MCP、后台 shell，以及应用预览网关。

## sandbox 里的浏览器工具

`browser` 内置贡献了 `browser_*` 工具包，但它只在**同时**满足两个条件时才挂载：

1. agent 激活了 `browser`（`"browser" in Options.plugins`），并且
2. 会话绑定到一个存活的 sandbox 容器。

在官方预设里，只有 `web` 子代理开启了 `browser`；`main` 保持无浏览器，并把页面相关的工作委派给它。用 `sandbox_browser_options()` 开启：

```python
from noeta.sdk import presets

options = presets.sandbox_browser_options()   # main + the web subagent, browser on
```

## 各项开关

| `HostConfig` 字段 | 作用 |
| --- | --- |
| `sandbox_exec_preamble` | `(exec_env_ref, argv) -> str` —— 为每条命令前置一段 shell 前缀（例如临时签发一份凭证）。每条命令都会重新调用。 |
| `sandbox_backend_factory` / `sandbox_browser_factory` | 在不动接缝的前提下换掉容器线。 |
| `sandbox_policy` | `(root_task_id, workspace_dir) -> bool` —— 按会话退出 sandbox；返回 `False` 则回落到 `LocalExecEnv`。 |

## 已知边界

- 库**不提供置备器** —— 跑 `docker`、调 K8s API 或对接远程会话服务，都是 host 的活。开箱的 provider 只做 attach。
- sandbox 的副作用在 worker 世代之间**没有围栏**：一个 lease 已过期的 worker 仍然可以往容器 `POST`。它受限于与崩溃 Step 副作用相同的那套 Step attempt 重驱动机制。
- 在 sandbox 下，`shell_run` 的 `timeout` 是客户端侧强制的 —— 调用返回之后，命令仍在容器里继续跑。

## 下一步

- [用 Docker 部署](docker-deployment.md) —— 让 host 自己也跑在容器里，与 sandbox 并存
- [已知限制](../operations/limitations.md) —— sandbox 各项边界的细节
- [SDK 参考](../reference/sdk.md) —— 完整的 sandbox 面
- [ADR：execution environment seam](https://github.com/initxy/noeta/blob/main/docs/adr/execution-environment-seam.md) —— 设计取舍
