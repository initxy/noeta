# 按租户隔离记忆

一个 `Client` 服务很多终端用户，每个租户的长期记忆放在各自的目录里：召回、记忆工具、记忆整理都不会跨租户。Noeta 只认任务，不认用户，所以任务属于哪个租户由你的后端告诉它。

| `HostConfig` 字段 / 参数 | 签名 | 管什么 |
| --- | --- | --- |
| `memory_root_resolver` | `(task_id) -> Path \| None` | 任务读写哪个记忆目录 |
| `mcp_scope_resolver` | `(task_id) -> str \| None` | 哪些任务可以共用同一条 MCP 连接 |
| `run_consolidation(include_task=...)` | `(task_id) -> bool` | 一次整理处理哪些任务 |

单租户的 host 这些都不用设。

## 把任务映射到租户目录

```python
from pathlib import Path

from noeta.sdk import Client, HostConfig, presets
from noeta.sdk.providers import AnthropicProvider

TENANT_ROOTS = Path("/var/lib/myapp/memories")   # one subdirectory per tenant
task_tenants: dict[str, str] = {}                # task_id -> tenant; your DB in production

def memory_root_for(task_id: str) -> Path | None:
    tenant = task_tenants.get(task_id)
    return TENANT_ROOTS / tenant if tenant else None

client = Client(
    presets.with_consolidation_agent(presets.main_options()),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    host_config=HostConfig(
        storage_path="/var/lib/myapp/noeta.sqlite",
        memory_root_resolver=memory_root_for,
        mcp_scope_resolver=task_tenants.get,     # tenant id as the MCP scope
    ),
)

print(client.memory_root(some_task_id))
```

```
/var/lib/myapp/memories/acme-corp
```

- 这个函数要快、不能抛异常、同一个任务 id 每次都给同样的结果：每一轮都会调用它，任务恢复后也必须找到同一个目录。
- 返回 `None` 就用 host 级别的默认顺序：`memory_dir` > `global_memory_dir` > `~/.noeta/memories`。
- 每一轮都按任务对应的目录重新构建引擎，所以两个租户永远不会用到同一份记忆。
- 不设 `mcp_scope_resolver` 的话，两个租户的 MCP 配置一模一样时会共用一条连接，有状态的服务器（浏览器、登录态）就会把一个租户的状态带进另一个租户的对话。

## 第一轮怎么登记

任务 id 是在 `start` / `seed_start` 里面生成的，事先查不到。有两种办法：

- **先 seed，再登记，再跑。** 调 `seed_start`，记下 `task_tenants[seeded.task_id] = tenant`，然后 `drive_seeded` 或 `dispatch_seeded`。这中间没有 worker 能构建这一轮的引擎。seed 过程本身的召回走默认顺序，所以把 `global_memory_dir` 指向一个空目录。
- **从工作区推出来。** 用 `start(goal=..., workspace_dir=...)` 传入租户的工作区。这个路径在第一次召回之前就记到任务上了，你的函数可以读回来，再从工作区对应到租户。

## 按租户整理记忆

每个租户单独跑一次整理。client 的 `Options` 里必须有 `__consolidation__` agent（像上面那样用 `presets.with_consolidation_agent`）：

```python
from noeta.sdk import run_consolidation

def consolidate_tenant(tenant: str) -> bool:
    return run_consolidation(
        client,
        memory_root=TENANT_ROOTS / tenant,
        include_task=lambda tid: task_tenants.get(tid) == tenant,
        on_seeded=lambda tid: task_tenants.__setitem__(tid, tenant),
    )
```

- `on_seeded` 会在任何 worker 接手之前把整理任务的 id 给你，把它登记进映射，整理 agent 的 `memory_*` 工具才会写到同一个租户目录。
- 防抖标记放在各租户自己的目录里，租户之间互不影响。
- `include_task` 之外的任务完全不进摘要。

如果只让整理任务写记忆，就设 `HostConfig(memory_read_only=True)`：agent 只有 `memory_read` 和 `memory_search`，系统提示里的记忆说明也换成只读版（不再教它怎么写），整理 agent 仍然有全部四个记忆工具。

## 要注意的

- 这里的隔离是目录隔离，不是权限控制。把这些目录放在你的服务自己管的路径下。
- 映射不到租户的任务会退回共享目录。严格的多租户部署里，把这个退回目录设成一个空的、有监控的目录。
- 子 agent 用自己的任务 id 查目录。官方预设只在 `main` 上开了记忆；如果你给自定义子 agent 开了记忆，函数里也要能处理子任务 id（比如用 `client.task_status(tid).parent_task_id` 一路找到根任务）。

## 下一步

- [上生产](deploy.md)：整理任务跑在 worker 池上
- [接入 MCP](mcp.md)：`mcp_scope_resolver` 分的就是这些连接
- [ADR：memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md)
