# 多租户记忆

**目标：** 用一个常驻 `Client` 服务属于不同终端用户的会话，让每个租户的长期记忆放在各自的存储里——召回、记忆工具、后台 consolidation 全部按租户隔离。

**开始之前：** 你已经通过[你的第一个代理](../tutorials/first-agent.md)了解 SDK，并了解 Memory v2（政策 prompt、召回与 consolidation，见 `docs/adr/memory-consolidation.md`）。

## 两个 seam

SDK 保持对租户无感——它只认识任务，不认识用户。两个宿主侧 seam 让你的后端来决定任务 → 租户的映射：

1. **按任务解析记忆根目录** —— `HostConfig.memory_root_resolver`，一个 `(task_id) → Path | None` 可调用对象。设置后，记忆根目录链的每个消费方都先经过它解析：引擎构建（记忆工具包 + 常驻索引）、goal 时的召回、`Client.memory_root`。返回 `None` 时回落到既有链（`memory_dir` > `global_memory_dir` > `~/.noeta/memories`）。
2. **consolidation 摘要按租户过滤** —— `run_consolidation(..., include_task=...)`，一个针对根会话 task id 的谓词，让一次 curation 只消化一个租户的会话。

单租户宿主什么都不用改：不设 resolver、不设过滤器，行为与今天完全一致。

## 接线 resolver

```python
from pathlib import Path
from noeta.sdk import Client, HostConfig, Options

TENANT_ROOTS = Path("/var/lib/myapp/memories")  # 每个租户一个子目录
task_tenants: dict[str, str] = {}               # task_id → 租户；生产环境放你的数据库

def memory_root_for(task_id: str) -> Path | None:
    tenant = task_tenants.get(task_id)
    return TENANT_ROOTS / tenant if tenant else None

client = Client(
    options,
    provider=provider,
    workspace_dir=workspace,
    host_config=HostConfig(
        event_log=event_log, content_store=content_store, dispatcher=dispatcher,
        memory_root_resolver=memory_root_for,
    ),
)
```

resolver 必须**廉价、全函数、且对同一 task id 确定**——它跑在引擎构建与 goal 路径上，恢复的任务必须解析到同一个存储。

## 映射首轮

新会话的 task id 是在 `start` / `seed_start` 内部铸出来的，单纯的字典查找此时还不认识它。两种策略：

- **从持久化记录推导。** 首轮召回运行之前，创世 `TaskCreated` 与 `TaskHostBound` 的 workspace 绑定已经写入，所以 resolver 可以从 ledger 读出会话的 workspace，再做 workspace → 租户的映射（每个租户有独立 workspace 目录时最自然）。
- **先 seed、再注册、后 drive。** 如果你的后端自己驱动 turn（异步的 `seed_start` → `drive_seeded` 拆分），在两个调用之间注册映射——seed 的 lease 仍被持有，映射就位前没有 worker 能解析引擎。这种策略下，第一条 goal 的 *seed 时* 召回仍会回落到宿主级链；把回落目录（`global_memory_dir`）指向一个空目录，让它召回不到任何东西。

引擎按解析出的根目录分片缓存：两个租户永远不会共享一个缓存引擎的记忆存储，而 resolver 回落的槽位与之前完全一致地共享。

## 按租户 consolidation

每个租户跑一次 pass。防抖 marker 存在各租户自己的根目录下，所以租户之间独立防抖；`on_seeded` 会在任何 worker 认领之前把 curation 任务的 id 交给你——把它注册进映射，curation agent 的 `memory_*` 工具就落在同一个租户存储里：

```python
from noeta.sdk import run_consolidation

def consolidate_tenant(tenant: str) -> bool:
    root = TENANT_ROOTS / tenant
    return run_consolidation(
        client,
        memory_root=root,
        include_task=lambda tid: task_tenants.get(tid) == tenant,
        on_seeded=lambda tid: task_tenants.__setitem__(tid, tenant),
    )
```

`include_task` 直接把租户范围之外的会话排除在摘要宇宙之外——既不消耗会话上限，也不计入 omitted——摘要头部会声明它被限定在宿主选定的会话子集内。

## 注意事项

- 记忆存储是文件系统材料：按租户隔离是目录隔离，不是鉴权层。把根目录放在你的服务自己拥有的目录下。
- resolver 无法映射的 memory-enabled 任务会回落到共享链。严格的多租户部署里，把回落根目录当作隔离区（空目录、有监控），而不是真实存储。
- 委托出的子代理用自己的 task id 解析。官方预设只在 `main` 上启用记忆，子代理不会碰存储；如果你在自定义子代理上启用记忆，让 resolver 也能映射子任务 id（例如沿 ledger 走到根会话）。
