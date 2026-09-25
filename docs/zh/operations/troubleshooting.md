# 故障排查

每条都是：看到什么现象、运行时实际做了什么、该改什么。如果碰到的是设计上的边界而不是故障，看[已知限制](limitations.md)。

## 被拦下了

### Task 因预算超限而结束

- **现象：** Task 结束，原因类似 `max_iterations=5 exceeded` 或 `max_tool_calls=3 reached`。
- **原因：** `BudgetGuard` 发现某项预算超了，拒绝了下一步。预算项有 `max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks`、`max_subtask_depth`。
- **解决：** 先看 Task 的事件日志，确认是哪一项触发的；再通过 `Options.budget` 传一个 `BudgetSpec` 调高上限，或者把任务拆小。`max_cost_usd` 只对模型目录里有价格的模型生效（见下文「悄悄变差了」）。

### 工具调用被拒

- **现象：** 出现 `ToolCallDenied` 事件，原因是 `tool 'X' denied by policy`、`tool 'X' not in allowlist` 或 `tool 'X' risk_level 'high' exceeds max 'medium'`。
- **原因：** `PermissionGuard` 先查 `denied_tools`（来自 `Options.disallowed_tools`）和 `allowed_tools` 白名单，再拿工具的 `risk_level` 跟这个 agent 允许的上限比。
- **解决：** 把工具加进 `Options.allowed_tools`（注意它会**替换**默认列表，不是追加），或者从 `disallowed_tools` 里去掉。如果是风险等级超了，给这个工具单开一个 agent，别为了它把所有工具的上限都调高。

### 工具调用在等审批

- **现象：** Task 挂起，原因是 `tool 'X' requires human approval`（逐次判断的情况是 `tool 'X' call requires human approval`）。
- **原因：** 由 `permission_mode` 决定。`default` 下所有 `risk_level` 不是 `low` 的工具都要审批；`acceptEdits` 相同，但 `Edit` 和 `Write` 不用；`bypassPermissions` 什么都不拦。`Bash` 的命令不在 shell 白名单里时，无论哪种模式都要逐次审批。
- **解决：** 用 `Client.approve` / `Client.deny` 处理，或者在代码里用 `Options.can_use_tool` 自动裁决。如果这一类调用本来就该直接放行，换一个 `permission_mode`。

### 写文件被拒：路径在工作区之外

- **现象：** `Edit` 或 `Write` 报错，说路径落在工作区或可写目录之外。
- **原因：** 写操作只允许落在 Task 的工作区里。路径会先规范化（`..` 和符号链接都会展开），并且按路径分段比较，所以 `/srv/app-old` 不算在 `/srv/app` 里面。读操作不受限。
- **解决：** 写到工作区里，或者通过 `HostConfig.write_roots` 授权目录。它是一个 `task_id -> 目录列表` 的函数，每次调用都会重新查，所以 Task 暂停期间加的授权，恢复后马上生效。

## 该发生的没发生

### 挂起的 Task 一直不醒

- **现象：** Task 一直是 `suspended`，但它等的条件看上去已经满足了。
- **原因：** 三种可能：唤醒事件还没发生（定时器的 `fire_at` 还没到，子任务还没结束）；事件发生了，但跟 Task 的 `WakeCondition` 对不上；或者根本没有 worker 在处理队列。
- **解决：** 查定时器的 `fire_at` 或子任务状态，看一下 Task 的原始事件流，并确认有 worker 在跑。没人会替你启动 worker（见[部署](../guides/deploy.md)）。

## 配置被拒

### 报 "unknown plugin activation"

- **现象：** `compile_options` 抛出 `ValueError: unknown plugin activation 'x' on ...`。
- **原因：** `Options.plugins` 或 `AgentDefinition.plugins` 里的名字既不是内置插件，也不在传给 `Client` 的 `PluginSet` 里。拼错了会直接报错，免得某项能力悄悄被关掉。
- **解决：** 改正拼写，或者先 `load_plugins(...)`，再用 `Client(options, plugins=...)` 传进去。报错信息里会列出所有合法名字。

### 模型或 provider 在开跑前被拒

- **现象：** 抛出 `ModelSelectorError`（`model_selector_rejected`）或 `ProviderSelectorError`（`provider_selector_rejected`），不会写入任何 Task。
- **原因：** 模型不在 `principal.allowed_models` 与部署白名单的交集里；或者 `(provider, model)` 指向了没配置的 provider，或该 provider 没声明的模型。
- **解决：** 从错误上附带的 `allowed` / `available` 列表里挑一个，或者放宽 host 的白名单和 provider 注册。

### 401 或其他鉴权错误

- **现象：** 每一轮都因为 LLM 接口的鉴权或权限错误失败。
- **原因：** API key 没配、过期了，或者没有这个模型的权限。
- **解决：** 检查传给 provider 的 key，或它读取的环境变量。走代理的话设 `HTTPS_PROXY`，provider 用的 `httpx` 会读它。

### 接口报 "Model not found"

- **现象：** provider 返回模型不存在。
- **原因：** `model` 不是这个接口认识的 id。
- **解决：** 传接口实际提供的完整 id（比如 `claude-sonnet-5`；Noeta 认识的 id 可以用 `catalog_models()` 查），并确认 key 的权限等级。

## 悄悄变差了

### 目录里没有的模型：告警且成本为 $0

- **现象：** 日志里有一次性的 `noeta` 告警，点名这个模型；即使模型实际窗口更大，压缩也按 128K 算；`GovernanceState.cost` 一直是 0。
- **原因：** 压缩、输出上限和计价都来自模型目录。不认识的模型会按 128,000 token 窗口、16,384 token 输出上限、价格 `0.0` 处理，每项在日志里提示一次。
- **解决：** 给它注册一条 `ModelSpec`：`HostConfig(extra_models={...})`，或 `noeta.sdk.providers` 里的 `register_models`。见[接入模型](../guides/models.md)。

## worker 异常

### 关闭时放弃了正在跑的一步

- **现象：** SIGTERM 之后日志出现 `shutdown_abandoned`，`loop.abandoned` 为 `True`。
- **原因：** 正在跑的那一步超过了 `shutdown_grace_s`（`WorkerLoop` 默认 30 秒，`Client.start_workers` 默认 10 秒）。
- **解决：** 直接退出进程。Python 停不掉被放弃的线程，在同一进程里继续用这个 loop 是不支持的。进程退出后租约会过期，下次启动时 `requeue_stale()` 会把 Task 捡回来。想避免这种情况就调大 `shutdown_grace_s`，或设为 `None` 无限等待（那样卡死的一步只能 `kill -KILL <pid>`）。

### 跑得很久的一步报 `InvalidLease`

- **现象：** 一个跑了很久的步骤在下次写事件日志时失败，worker 发出了 `heartbeat_invalid_lease`。
- **原因：** 心跳续租次数有上限，就是 dispatcher 的 `heartbeat_max`（360），所以一步最多持有租约 `heartbeat_interval × heartbeat_max` 这么久。
- **解决：** 把它当成需要人去看的信号，不要指望它自动恢复。如果这一步确实就要跑这么久，调大 `heartbeat_interval` 或 `heartbeat_max`；否则去查是什么卡住了。

## 下一步

- [已知限制](limitations.md)
- [部署 worker](../guides/deploy.md)
- [`WorkerLoop` 参考](../reference/worker-loop.md)
