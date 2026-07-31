# 故障排查

实践中真正会出问题的地方，以及该怎么办。每一条都遵循同样的形状 —— **症状**（你看到什么）、**原因**（运行时实际做了什么）、**修法**（该改什么）。

如果你碰到的是设计的边界而不是故障，那它收录在[已知限制](limitations.md)里。

跳到匹配的那一组：

- [有东西被拦下了](#有东西被拦下了) —— 预算、权限、写入围栏
- [有东西一直不发生](#有东西一直不发生) —— 一个不肯唤醒的 Task
- [配置被拒绝](#配置被拒绝) —— 插件、模型、provider
- [有东西在静默降级](#有东西在静默降级) —— 目录里没有的模型
- [Worker 表现异常](#worker-表现异常) —— 关闭与 lease 相关的现象

## 有东西被拦下了

### Task 因预算拒绝而终止

**症状。** 一个 Task 以类似 `max_iterations=5 exceeded` 或 `max_tool_calls=3 reached` 的原因终止。

**原因。** `BudgetGuard` 拒绝了下一个动作，因为越过了某条配置的预算轴：`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks` 或 `max_subtask_depth`。这个 Task 确实运行过并产生了持久信封 —— 只是以不成功的方式终止了。

**修法。**

1. 读这个 Task 的事件日志，看是哪条轴触发的，以及为什么它需要这么多步。
2. 通过 `Options.budget` 传一个 `BudgetSpec` 来提高上限。
3. 或者收窄 Task 的范围，让它需要更少的步数。

`max_cost_usd` 只会对目录已定价的模型触发 —— 见[静默降级那一条](#长对话从不-compaction-成本停在-0-00)。

### 工具调用被 PermissionGuard 拒绝

**症状。** 一个 `ToolCallDenied` 事件，其原因是 `tool 'X' denied by policy`、`tool 'X' not in allowlist` 或 `tool 'X' risk_level 'high' exceeds max 'medium'` 之一。

**原因。** `PermissionGuard` 驳回了这次调用。先检查 Policy 的 `denied_tools` 集合（由 `Options.disallowed_tools` 喂入）和 `allowed_tools` 白名单，再用工具声明的 `risk_level` 对照这个 agent 运行时的上限。

**修法。** 扩大 `Options.allowed_tools` 以包含该工具，或把它从 `disallowed_tools` 里去掉 —— 记住 `allowed_tools` 是*替换*默认集合，而不是往里追加。如果拒绝与风险级别有关，说明该工具高于这个 agent 的上限：给它一个自己的 agent，而不是为所有东西抬高上限。

### 工具调用一直等待审批

**症状。** Task 挂起而不是运行工具；原因写着 `tool 'X' requires human approval`。

**原因。** `permission_mode` 选定了一个审批集合。`default` 对每一个声明 `risk_level` 不为 `low` 的工具设门；`acceptEdits` 应用同样的规则，但豁免 `edit`、`write` 和 `apply_patch`；`bypassPermissions` 不设任何门。一个命令落在生效 shell 白名单之外的 `shell_run` 会按调用设门，与模式无关。

**修法。** 用 `Client.approve` / `Client.deny` 处理它，或者用一个编程式的 `Options.can_use_tool` 回调，它的裁决会被记录成一个普通的审批事件。如果整类调用都应该无人值守地运行，就改 `permission_mode`。

### 写入被拒：路径解析到工作区之外

**症状。** `edit`、`write` 或 `apply_patch` 返回一个错误，说路径解析到工作区之外，或落在可写白名单之外。

**原因。** 写入工具通过 `WorkspaceRoot` 围栏解析。目标会被规范化 —— 因此 `..` 和符号链接逃逸已经被折叠 —— 并且必须落在会话工作区之内，或落在 host 授权的某个额外根目录之内。包含判断是按路径分量进行的，因此 `/srv/app-old` 不在 `/srv/app` 之内。读取不设围栏；只有写入设围栏。

**修法。** 写在工作区之内，或通过 `HostConfig.write_roots` 授权该目录 —— 它是一个 `task_id -> directories` 解析器，按调用查询。因为它按调用查询，一个在 Task 暂停期间授予的授权，会在恢复后的那次调用上生效，而无需重建工具集。

## 有东西一直不发生

### 挂起的 Task 永远不唤醒

**症状。** 一个 Task 停在 `suspended` 且从不回到 `running`，尽管它所等待的条件看起来已经满足。

**原因。** 三种情况之一：

- 唤醒事件尚未产生 —— 一个 `fire_at` 还在未来的定时器，或一个尚未到达终态的子任务。
- 唤醒事件已产生，但不匹配这个 Task 的 `WakeCondition`（身份字段上的投影不匹配）。
- 没有 worker 在排空队列。

**修法。**

1. 检查唤醒事件是否存在：对定时器核实 `fire_at` 已在过去；对子任务核实子任务已到终态。
2. 读这个 Task 的原始事件流。一个在等待尚未发生之事的 Task 是按设计工作的。
3. 确保有一个 `WorkerLoop` 在排空 Dispatcher —— 见[部署 Worker](../how-to/deploy-worker.md)。没有任何东西替你启动它。

## 配置被拒绝

### 编译失败并报 "unknown plugin activation"

**症状。** `compile_options` 抛出 `ValueError: unknown plugin activation 'x' on ... — not a built-in activation (...) and not in the loaded plugin set (...)`。

**原因。** `Options.plugins` 或 `AgentDefinition.plugins` 里的这个名字，既不是一个已识别的内置 activation，也不是交给 `Client` 的那个 `PluginSet` 里的插件。activation 名称按设计会大声失败，因此一个拼写错误不可能静默地关掉一项能力。

**修法。** 改正拼写，或者先用 `load_plugins(...)` 加载插件并把结果作为 `Client(options, plugins=...)` 传入。错误消息会同时列出已识别的内置名称和已加载的集合。

### 模型在这一轮开始前就被拒绝

**症状。** `ModelSelectorError`（`model_selector_rejected`）或 `ProviderSelectorError`（`provider_selector_rejected`）—— 并且没有 Task、没有 `ModelBound`、没有这一轮。

**原因。** 这些是在任何持久写入之前本地抛出的。要么选择器落在 `principal.allowed_models ∩` 部署白名单之外，要么 `(provider, model)` 这一对指向一个未配置的 provider，或指向一个该 provider 未声明的模型。

**修法。** 两个错误都携带一个 `allowed` / `available` 列表，说明你本可以挑选什么。从中挑选，或扩大 host 的白名单和 provider 注册表。

### provider 返回 401 或其他认证错误

**症状。** 轮次因来自 LLM 端点的认证或权限错误而失败。

**原因。** API key 缺失、过期，或无权访问所请求的模型。

**修法。** 核实传给 provider 适配器的 key，或它回退到的那个环境变量。在企业代理后面，在环境里设置 `HTTPS_PROXY` —— 适配器使用 `httpx`，它会尊重这个变量。

### 端点返回 "Model not found"

**症状。** provider 本身返回一个模型未找到或未知模型的错误。

**原因。** 你传入的 `model` 不是那个端点提供的 id。

**修法。** 使用一个你的端点确实提供的精确模型名。Anthropic 的 id 带日期后缀（`claude-sonnet-4-5-20250929`）；同时检查你这把 key 的访问层级。

## 有东西在静默降级

### 长对话从不 compaction，成本停在 $0.00

**症状。** 上下文一直增长，直到 provider 拒绝请求；而无论跑多少轮，`GovernanceState.cost` 都停在零。

**原因。** compaction 和定价都源自模型目录。目录未描述的模型每次往返会得到 `COMPACTION_OFF` 和 `0.0` 的价格。这两种退化都不抛异常，所以没有任何东西告诉你。

**修法。** 为该模型添加一行 `ModelSpec`。`CATALOG` 和 `ModelSpec` 都从 `noeta.sdk.providers` 重新导出；这一行的 `context_window`、`max_output_tokens` 和价格字段，就是两处推导所读取的全部内容。见[配置 Provider](../how-to/configure-provider.md)。

## Worker 表现异常

### 关闭时 Step 被放弃

**症状。** SIGTERM 之后，日志显示 `shutdown_abandoned` 且 `loop.abandoned` 为 `True`。

**原因。** 进行中的 Step 没有在 `shutdown_grace_s` 内完成 —— `WorkerLoop` 默认 30 秒，`Client.start_workers` 是 10 秒 —— 因此循环放弃了它。

**修法。**

- **退出进程。** Python 无法中断被放弃的 Step 线程，它可能仍在写入事件日志。放弃之后在进程内重用这个循环是不受支持的。
- 一旦进程退出，lease 就会过期，`requeue_stale()` 会在下一次启动时回收该 Task。
- 要避免它，提高 `shutdown_grace_s`，或把它设为 `None` 以无限等待 —— 那样一个真正卡住的 Step 就需要 `kill -KILL <pid>`。

### 一个长 Step 以 InvalidLease 死亡

**症状。** 一个已经运行很久的 Step 在它下一次写事件日志时失败；worker 发出了 `heartbeat_invalid_lease`。

**原因。** Dispatcher 把心跳延长次数限制在 `heartbeat_max`（默认 360），因此一个 Step 至多能持有 lease `heartbeat_interval × heartbeat_max` 那么久。超过上限后 lease 被强制释放，下一次经 lease 校验的追加就会失败。

**修法。** 把这当作一个运维故障信号，而不是恢复路径 —— 循环会继续前进，但这个 Task 需要检查。如果这个 Step 确实就是这么慢，就提高 `heartbeat_interval` 或 Dispatcher 的 `heartbeat_max`；否则找出是什么在挂着。

## 下一步

- [已知限制](limitations.md) —— 设计的边界，而不是 bug
- [部署 Worker](../how-to/deploy-worker.md) —— 上面多数现象的来源，那个 worker 池
- [唤醒与恢复](../concepts/wake-resume.md) —— 唤醒机制如何工作
- [WorkerLoop 参考](../reference/worker-loop.md) —— 构造函数参数与关闭语义
