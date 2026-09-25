# 在沙箱里跑工具

把 agent 读写文件、执行命令、上网和操作浏览器这些动作都放进容器里，agent 就碰不到宿主机。Noeta 负责和容器通信；容器的启动和销毁由你的 host 负责。

| 模式 | `HostConfig` 字段 | 适合 |
| --- | --- | --- |
| **连接现成容器**：大家共用一个 | `exec_env=SandboxExecEnvConfig(...)` | 开发、简单部署 |
| **按任务分配容器**：每个根任务一个 | `sandbox_provider=...`、`sandbox_spec=SandboxSpec(...)` | 生产环境隔离 |

两个都设时以 `sandbox_provider` 为准。

## 连接一个已经在跑的容器

自己先起一个容器（比如 [AIO Sandbox](https://github.com/bytedance/aio-sandbox) 镜像），再把地址告诉 Noeta：

```python
from noeta.sdk import Client, HostConfig, Options, SandboxExecEnvConfig
from noeta.sdk.providers import AnthropicProvider

client = Client(
    Options(system_prompt="You are a coding agent."),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    workspace_dir="./workspace",
    host_config=HostConfig(
        exec_env=SandboxExecEnvConfig(
            base_url="http://localhost:8080",
            api_key_env="SANDBOX_API_KEY",   # read at connect time, never stored
            workdir="/workspace",
        ),
    ),
)
```

```bash
export SANDBOX_API_KEY=your-container-key
```

让 agent 跑一下 `hostname`，打印出来的是容器 id，不是你机器的主机名。所有任务共用这一个容器。

## 每个任务分配一个容器

实现 `SandboxProvider` 协议的三个方法：

```python
import subprocess

from noeta.sdk import SandboxHandle, SandboxSpec, StaticApiKeyAuth, decode_exec_env_ref


class DockerSandboxProvider:
    def allocate(self, root_task_id: str, spec: SandboxSpec) -> SandboxHandle:
        # Called once when a root task opens: start a fresh container.
        mounts = [a for m in spec.mounts for a in ("-v", f"{m.source}:{m.target}:{m.mode}")]
        container_id = subprocess.run(
            ["docker", "run", "-d", "--rm", *mounts, spec.image, "sleep", "infinity"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Wait until it is ready, then return its API address.
        return SandboxHandle(
            base_url="http://localhost:8080",
            sandbox_id=container_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        # Called on resume, possibly on another host: reconnect, never re-create.
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth(env_name="SANDBOX_API_KEY"),
            workdir="/workspace",
        )

    def release(self, root_task_id: str) -> None:
        # Called when the root task ends. Must be idempotent.
        ...  # look up the container for root_task_id and stop it
```

```python
from noeta.sdk import HostConfig, MountSpec, SandboxSpec

host_config = HostConfig(
    sandbox_provider=DockerSandboxProvider(),
    sandbox_spec=SandboxSpec(
        image="aio-sandbox:latest",
        mounts=(MountSpec(source="/srv/skills", target="/skills", mode="ro"),),
        resources={"memory": "2g", "cpus": "2"},
    ),
)
```

- 容器地址以 `"{base_url}#{sandbox_id}"` 的形式记在任务上。崩溃恢复后，任务通过 `attach` 连回**同一个**容器。凭证不会被记下来，`StaticApiKeyAuth` 每次从环境变量里读。
- `SandboxSpec` 的字段：`image`、`mounts`、`resources`（原样交给你的分配逻辑）、`env`。任务工作区的挂载会在 `allocate` 时自动加进 `spec.mounts`，这里只写额外的挂载。
- `MountSpec.kind` 可以是 `"local-path"`（默认）、`"volume"`、`"nas"` 或 `"pvc"`，方便集群环境把挂载映射到自己的存储上。

## 哪些在容器里跑，哪些留在 host

| 在容器里 | 在 host 上 |
| --- | --- |
| `Read`、`Write`、`Edit`、`Glob`、`Grep` | `memory_*` 工具 |
| 前台 `Bash` | 后台 shell（沙箱下直接拒绝） |
| `WebFetch`、`WebSearch`（出网范围由容器的网络策略决定） | MCP 服务器 |
| 技能索引 | 应用预览网关 |
| `browser_*` 工具 | |

`WebFetch` 要不要审批，仍然在 host 上按 URL 的域名判断，见[内置工具](../reference/tools.md)。

## 打开浏览器工具

`browser_*` 工具要同时满足两个条件才会出现：agent 启用了 `browser`，**并且**任务绑定了一个活着的沙箱。官方预设把浏览交给一个 `web` 子 agent：

```python
from noeta.sdk import presets

options = presets.sandbox_browser_options()   # main + the web subagent
```

## 其他开关

| `HostConfig` 字段 | 作用 |
| --- | --- |
| `sandbox_exec_preamble` | `(exec_env_ref, argv) -> str`，每条命令前面加一段 shell 前缀（比如现取一个凭证） |
| `sandbox_policy` | `(root_task_id, workspace_dir) -> bool`，返回 `False` 的任务在本地跑 |
| `sandbox_backend_factory` / `sandbox_browser_factory` | 换掉和容器通信的客户端 |

## host 自己也跑在 Docker 里

- **挂 Docker socket：** 把 `/var/run/docker.sock` 挂进 host 容器，上面的 provider 就能起同级容器。
- **sidecar：** 沙箱作为同一个 compose 里的另一个服务，用 `SandboxExecEnvConfig(base_url="http://sandbox:8080")` 连过去。

## 限制

- 沙箱里的调用不受任务租约约束：租约已过期的 worker 在发现之前仍然能访问容器。
- `Bash` 的 `timeout` 在客户端计时，调用返回后命令在容器里还会继续跑。

## 下一步

- [上生产](deploy.md)：worker、Postgres 和 Docker
- [已知限制](../operations/limitations.md)：沙箱边界的细节
- [ADR：execution environment](https://github.com/initxy/noeta/blob/main/docs/adr/execution-environment-seam.md)
