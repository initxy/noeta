# 按租户隔离记忆

本指南教你用一个常驻 `Client` 服务多个终端用户，并让每个租户的长期记忆落在各自的存储里 —— recall、记忆工具和后台整理全部按租户划定范围。你需要[你的第一个 agent](../tutorials/first-agent.md) 里的 SDK 基础，以及一个已经知道请求属于哪个用户的后端。

## 两条接缝

SDK 对租户是无感的：它只认识 Task，不认识用户。两条 host 侧的接缝让你的后端拥有 Task → 租户的映射。

1. **按 Task 解析记忆根目录** —— `HostConfig.memory_root_resolver`，一个 `(task_id) → Path | None` 的可调用对象。记忆根目录链条的每一个消费方都先经它解析：引擎构建（记忆工具包 + 常驻索引）、目标时刻的 recall，以及 `Client.memory_root(task_id)`。返回 `None` 则回落到 host 级别的链条，`memory_dir` > `global_memory_dir` > `~/.noeta/memories`。
2. **整理摘要的范围** —— `run_consolidation(..., include_task=...)`，一个作用在根会话 Task id 上的谓词，因此一次整理只消化一个租户的会话。

单租户 host 两者都不设，直接拿到 host 级别的链条。

## 1. 接上解析器

```python
from pathlib import Path
from noeta.sdk import Client, HostConfig

TENANT_ROOTS = Path("/var/lib/myapp/memories")  # one subdirectory per tenant
task_tenants: dict[str, str] = {}               # task_id → tenant; your DB in production

def memory_root_for(task_id: str) -> Path | None:
    tenant = task_tenants.get(task_id)
    return TENANT_ROOTS / tenant if tenant else None

client = Client(
    options,
    provider=provider,
    workspace_dir=workspace,
    host_config=HostConfig(
        storage_path="/var/lib/myapp/noeta.sqlite",
        memory_root_resolver=memory_root_for,
    ),
)
```

检查它是否按你预期的方式解析：

```python
print(client.memory_root(some_task_id))
```

```
/var/lib/myapp/memories/acme-corp
```

对每个 Task id 而言，这个解析器必须**廉价、全域且确定** —— 它跑在引擎构建路径和目标路径上，而一个被恢复的 Task 必须解析到同一个存储。

## 2. 映射第一轮

新会话的 Task id 是在 `start` / `seed_start` 内部生成的，所以一次简单的字典查询此刻还无从得知。有两种策略：

- **从持久记录里推导出来。** 把租户的工作区作为 `start(goal=..., workspace_dir=...)` 传入。驱动器会在 Task 创建过程中、也就是第一轮 recall 运行之前，把那个绝对路径焊到该会话的 `TaskHostBound` 事件上 —— 于是解析器可以从账本上读出工作区，并把工作区映射到租户。
- **先 seed、再注册、然后驱动。** 如果你的后端自己驱动每一轮（`seed_start` → `drive_seeded` / `dispatch_seeded` 这种拆分），就在这两次调用之间注册映射：seed 的 lease 被持有着，所以在映射存在之前没有 worker 能解析出引擎。seed 时刻的 recall 和 seed 时刻的常驻索引会走 host 级别的链条，因此把回退目标（`global_memory_dir`）指向一个空目录。

引擎按解析出来的根目录做缓存，所以两个租户永远不会共用某个被缓存引擎的记忆存储。

## 3. 按租户整理

每个租户跑一遍。先在配方上注册 `__consolidation__` agent —— `run_consolidation` 会以那个保留名字 seed 一个根 Task：

```python
from noeta.sdk import presets, run_consolidation

options = presets.with_consolidation_agent(options)

def consolidate_tenant(tenant: str) -> bool:
    root = TENANT_ROOTS / tenant
    return run_consolidation(
        client,
        memory_root=root,
        include_task=lambda tid: task_tenants.get(tid) == tenant,
        on_seeded=lambda tid: task_tenants.__setitem__(tid, tenant),
    )
```

去抖标记存放在每个租户各自的根目录里，因此各租户独立去抖。`on_seeded` 会在任何 worker 能认领这个整理 Task **之前**把它的 id 交给你 —— 把它注册进你的映射，好让整理 agent 的 `memory_*` 工具落在同一个租户存储里。

`include_task` 会把租户范围之外的会话彻底排除 —— 它们既不消耗会话上限，也不算作被省略 —— 而摘要的头部会声明它被限制在了 host 选定的一个子集上。

## 注意事项

- 记忆存储是文件系统层面的东西：按租户隔离是目录隔离，不是一层授权。把这些根目录放在你的服务自己拥有的目录之下。
- 一个启用了记忆、但解析器映射不出来的 Task 会回落到共享链条。在严格的多租户部署里，把这个回退根目录当作隔离区（保持为空、有监控），而不是一个真实的存储。
- 被委派的子代理用它们自己的 Task id 解析。官方预设只在 `main` 上启用记忆，所以子 agent 从不碰这个存储；如果你在某个自定义子 agent 上启用了记忆，就让你的解析器也能映射子 Task id（例如沿账本走到根 Task）。

## 下一步

- [部署 Worker](deploy-worker.md) —— 这一切所运行其上的常驻池
- [生成子代理](spawn-subagents.md) —— 为什么子 agent 用自己的 Task id 解析
- [SDK 参考](../reference/sdk.md) —— `HostConfig`、`run_consolidation`
- [ADR：memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md) —— 完整的整理流程
