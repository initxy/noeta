# 多租户记忆

**目标：** 用一个常驻 `Client` 服务属于不同终端用户的会话，让每个租户的长期记忆各自存放在独立的存储里——召回、记忆工具、后台 consolidation 全部按租户隔离。

**开始之前：** 你已经通过[你的第一个代理](../tutorials/first-agent.md)了解了 SDK，并了解 [Memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md) 中描述的那次 curation pass。

## 两个 seam

SDK 对租户无感——它只认识任务，不认识用户。两个宿主侧 seam 让你的后端来掌管任务 → 租户的映射。

1. **按任务解析记忆根目录** —— `HostConfig.memory_root_resolver`，一个 `(task_id) → Path | None` 可调用对象。记忆根目录链的每一个消费方都先经过它解析：引擎构建（记忆工具包 + 常驻索引）、goal 时的召回，以及 `Client.memory_root(task_id)`。返回 `None` 时回落到宿主级链：`memory_dir` > `global_memory_dir` > `~/.noeta/memories`。
2. **consolidation 摘要按租户过滤** —— `run_consolidation(..., include_task=...)`，一个针对根会话 task id 的谓词，让一次 curation pass 只消化一个租户的会话。

单租户宿主两者都不设，得到的就是宿主级链。

## 接线 resolver

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

resolver 对每个 task id 必须**廉价、全函数、且确定**——它跑在引擎构建路径和 goal 路径上，而恢复的任务必须解析到同一个存储。

## 映射首轮

新会话的 task id 是在 `start` / `seed_start` 内部铸出来的，单纯的字典查找此时还不认识它。两种策略：

- **从持久化记录推导。** 把租户的 workspace 作为 `start(goal=..., workspace_dir=...)` 传入。driver 会在任务创建时把那个绝对路径焊到会话的 `TaskHostBound` 事件上——早于首轮召回运行——于是 resolver 可以从 ledger 读出这个 workspace，再做 workspace → 租户的映射。
- **先 seed、再注册、后 drive。** 如果你的后端自己驱动 turn（`seed_start` → `drive_seeded` 拆分），就在这两次调用之间注册映射：seed 的 lease 仍被持有，所以在映射就位之前没有 worker 能解析引擎。seed 时的召回与 seed 时的常驻索引都经宿主级链解析，因此把回落目录（`global_memory_dir`）指向一个空目录。

引擎按解析出的根目录分片缓存，所以两个租户永远不会共享同一个缓存引擎的记忆存储。

## 按租户 consolidation

每个租户跑一次 pass。先把 `__consolidation__` 代理注册到配方上——`run_consolidation` 会以这个保留名字 seed 出一个根任务：

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

防抖 marker 存在各租户自己的根目录下，所以租户之间独立防抖。`on_seeded` 会在任何 worker 认领之前把 curation 任务的 id 交给你——把它注册进你的映射，curation 代理的 `memory_*` 工具就会落在同一个租户存储里。

`include_task` 会把租户范围之外的会话彻底排除——它们既不消耗会话上限，也不计入 omitted——而摘要头部会声明它被限定在宿主选定的会话子集内。

## 注意事项

- 记忆存储是文件系统材料：按租户隔离是目录隔离，不是鉴权层。把根目录放在你的服务自己拥有的目录下。
- 一个启用了记忆、但其任务 resolver 无法映射的代理会回落到共享链。在严格的多租户部署里，把回落根目录当作一个隔离区（空目录、有监控），而不是真实存储。
- 委托出去的子代理用它们自己的 task id 解析。官方预设只在 `main` 上启用记忆，所以子代理从不碰存储；如果你在自定义子代理上启用了记忆，就让你的 resolver 也能映射子任务 id（例如沿 ledger 走到根会话）。
