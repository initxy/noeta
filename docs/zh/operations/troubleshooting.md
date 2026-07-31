# 故障排查

常见问题及其解决方法。每一条遵循**症状 → 原因 → 解决方案**。

## 任务因预算拒绝而终止

**症状：** 任务以类似 `max_iterations=5 exceeded` 或 `max_tool_calls=3 reached` 的原因终止。

**原因：** `BudgetGuard` 拒绝了下一个动作，因为越过了某条配置的预算轴：`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks` 或 `max_subtask_depth`。任务确实运行过并产生了持久信封——只是以不成功的方式终止了。

**解决方案：**

1. 读取任务的 EventLog，看是哪条轴触发的，以及任务为什么需要这么多 Step。
2. 通过 `Options.budget` 传入一个 `BudgetSpec` 来提高上限。
3. 或收窄任务范围，让它需要更少的 Step。

注意 `max_cost_usd` 只会对目录已定价的模型触发——见下面"长对话从不 compaction"。

## 工具调用被 PermissionGuard 拒绝

**症状：** 一个 `ToolCallDenied` 事件，其原因是 `tool 'X' denied by policy`、`tool 'X' not in allowlist` 或 `tool 'X' risk_level 'high' exceeds max 'medium'` 之一。

**原因：** `PermissionGuard` 驳回了该调用。先检查 `denied_tools` 和 `allowed_tools` 白名单，再用工具声明的 `risk_level` 对照 Policy 的上限。

**解决方案：**

- 扩大 `Options.allowed_tools` 以包含该工具，或把它从 `disallowed_tools` 中去掉。记住设置 `allowed_tools` 是*替换*默认集合，而不是往里追加。
- 如果拒绝与风险级别有关，说明该工具高于这个 Agent 运行的上限；给它一个自己的 Agent，而不是为所有东西抬高上限。

## 工具调用一直等待审批

**症状：** 任务挂起而不是运行工具；原因写着 `tool 'X' requires human approval`。

**原因：** `permission_mode` 选定了一个审批集合。`default` 对每一个声明 `risk_level` 不为 `low` 的工具设门。`acceptEdits` 应用同样的规则，但豁免 `edit`、`write` 和 `apply_patch`。`bypassPermissions` 不设任何门。一个命令落在生效 shell 白名单之外的 `shell_run` 会按调用设门，独立于模式之外。

**解决方案：** 处理它——`Client.approve` / `Client.deny`，或一个编程式的 `Options.can_use_tool` 回调，其裁决会被记录为一个普通的审批事件。如果整类调用都应无人值守地运行，就改 `permission_mode`。

## 挂起的任务永远不唤醒

**症状：** 一个任务停在 `suspended` 且从不回到 `running`，尽管它所等待的条件看起来已经满足。

**原因：** 三种情况之一：

- 唤醒事件尚未产生——一个 `fire_at` 在未来的计时器，或一个尚未到达终态的子任务。
- 唤醒事件已产生但不匹配任务的 `WakeCondition`（身份字段上的投影不匹配）。
- 没有 Worker 在排空队列。

**解决方案：**

1. 检查唤醒事件是否存在：对计时器核实 `fire_at` 已在过去；对子任务核实子任务已终态。
2. 读取任务的原始事件流。一个在等待尚未发生之事的任务是按设计工作。
3. 确保有一个 `WorkerLoop` 在排空 dispatcher——见[部署一个 Worker](../how-to/deploy-worker.md)。没有任何东西替你启动它。

## 编译失败并报 "unknown plugin activation"

**症状：** `compile_options` 抛出 `ValueError: unknown plugin activation 'x' on ... — not a built-in activation (...) and not in the loaded plugin set (...)`。

**原因：** Activation 名称按设计会大声失败，因此一个拼写错误不会静默地关掉一项能力。`Options.plugins` 或 `AgentDefinition.plugins` 里的这个名字既不是一个已识别的内置 Activation，也不是交给 `Client` 的 `PluginSet` 里的插件。

**解决方案：** 改正拼写，或先加载插件——`load_plugins(...)` 并把结果作为 `Client(options, plugins=...)` 传入。错误消息会同时列出已识别的内置名称和已加载的集合。

## Provider 返回 401 或其他认证错误

**症状：** 轮次因来自 LLM 端点的认证或权限错误而失败。

**原因：** API 密钥缺失、过期，或无权访问所请求的模型。

**解决方案：** 核实传给 provider 适配器的密钥。在企业代理后面，在环境里设置 `HTTPS_PROXY`。

## 模型在轮次开始前就被拒绝

**症状：** `ModelSelectorError`（`model_selector_rejected`）或 `ProviderSelectorError`（`provider_selector_rejected`）——并且没有任务、没有 `ModelBound`、没有轮次。

**原因：** 这些是在任何持久写入之前本地抛出的。选择器落在 `principal.allowed_models ∩` 部署白名单之外，或者 `(provider, model)` 这一对指向一个未配置的 provider，或指向一个该 provider 未声明的模型。

**解决方案：** 两个错误都携带一个 `allowed` / `available` 列表，说明你本可以挑选什么。从中挑选，或扩大 host 的白名单和 provider 注册表。

## 端点返回 "Model not found"

**症状：** provider 本身返回一个模型未找到或未知模型的错误。

**原因：** 你传入的 `model` 不是那个端点提供的 id。

**解决方案：** 使用一个你的端点确实提供的精确模型名。Anthropic 的 id 带有日期后缀（`claude-sonnet-4-5-20250929`）；检查你的密钥的访问层级。

## 长对话从不 compaction，且成本停在 $0.00

**症状：** 上下文一直增长直到 provider 拒绝请求，且无论运行多少轮，`GovernanceState.cost` 都停在零。

**原因：** compaction 和定价都源自模型目录。目录未描述的模型会得到 `COMPACTION_OFF` 和每次往返 `0.0` 的价格。这两种退化都不会抛出异常，所以没有任何东西告诉你。

**解决方案：** 为该模型添加一行 `ModelSpec`。`CATALOG` 和 `ModelSpec` 从 `noeta.sdk.providers` 重新导出；这一行的 `context_window`、`max_output_tokens` 和价格字段就是两处推导所读取的全部内容。

## 写入被拒：路径解析到工作区之外

**症状：** `edit`、`write` 或 `apply_patch` 返回一个错误，说路径解析到工作区之外，或落在可写白名单之外。

**原因：** 写入工具通过 `WorkspaceRoot` 栅栏解析。目标会被规范化——因此 `..` 和符号链接逃逸已经被折叠——并且必须落在会话工作区之内，或落在 host 授权的一个额外根目录之内。包含判断是按路径分量进行的，因此 `/srv/app-old` 不在 `/srv/app` 之内。读取不设栅栏；只有写入设栅栏。

**解决方案：** 写在工作区之内，或通过 `HostConfig.write_roots` 授权该目录——它是一个 `task_id -> directories` 解析器，按调用查询。因为它按调用查询，一个在任务暂停期间授予的授权，会在恢复的调用上生效，而无需重建工具集。

## WorkerLoop：关闭时 Step 被放弃

**症状：** SIGTERM 之后，日志显示 `shutdown_abandoned` 且 `loop.abandoned` 为 `True`。

**原因：** 进行中的 Step 没有在 `shutdown_grace_s`（`WorkerLoop` 默认 30 秒，`Client.start_workers` 为 10 秒）内完成，因此循环放弃了它。

**解决方案：**

- **退出进程。** Python 无法中断被放弃的 Step 线程，它可能仍在写入 EventLog。放弃之后在进程内重用循环是不受支持的。
- 一旦进程退出，lease 过期，`requeue_stale()` 在下一次启动时回收该任务。
- 要避免它，提高 `shutdown_grace_s`，或把它设为 `None` 以无限等待——这时一个真正卡住的 Step 需要 `kill -KILL <pid>`。

## 一个长 Step 以 InvalidLease 死亡

**症状：** 一个已经运行很久的 Step 在它下一次写 EventLog 时失败；Worker 发出了 `heartbeat_invalid_lease`。

**原因：** Dispatcher 把心跳延长次数限制在 `heartbeat_max`（默认 360），因此一个 Step 至多能持有 lease `heartbeat_interval × heartbeat_max`。超过上限后 lease 被强制释放，下一次经 lease 校验的追加会失败。

**解决方案：** 把这当作一个运维故障信号，而不是恢复路径——循环会继续前进，但该任务需要检查。如果这个 Step 确实就是这么慢，就提高 `heartbeat_interval` 或 dispatcher 的 `heartbeat_max`；否则找出是什么在挂起。

## 另见

- [已知限制](limitations.md)——设计的边界，而非 bug
- [唤醒与恢复](../concepts/wake-resume.md)——唤醒机制如何工作
- [WorkerLoop 参考](../reference/worker-loop.md)——构造函数参数与关闭语义
