# 故障排查

常见问题及其解决方法。每条目遵循**症状 → 原因 → 解决方案**。

## 任务失败并报 "max_iterations exceeded"

**症状：** 任务以预算拒绝原因终止，如 `"max_iterations=5 exceeded"` 或 `"max_tool_calls=3 reached"`。

**原因：** `BudgetGuard` 拒绝了下一个动作，因为超过了配置的预算轴（迭代、工具调用、成本、生成的子任务）。任务仍然运行并产生了持久化信封——只是以不成功的方式终止。

**解决方案：**
1. 检查任务的 EventLog，看哪个预算轴触发了，以及任务为什么需要这么多步骤。
2. 通过 `Options.budget` 传递 `BudgetSpec` 来提高预算。
3. 或收窄任务范围以减少所需步骤。

## 工具调用被 PermissionGuard 拒绝

**症状：** 你的代理尝试使用工具并收到 `ToolCallDenied` 事件；trace 显示拒绝原因。

**原因：** `PermissionGuard` 拒绝了工具调用，因为该工具不在代理的 `allowed_tools` 集合中，或 `permission_mode` 要求对该风险级别进行显式批准。

**解决方案：**
- 在你的 `Options` 中扩大 `allowed_tools` 以包含该工具。
- 或以编程方式处理审批（`Options.can_use_tool`，或 `Client.approve` / `deny` 动词）。

## 挂起的任务永远不会唤醒

**症状：** 任务处于 `suspended` 状态但永远不会转换为 `running`，尽管它等待的条件似乎已经满足。

**原因：** 几种可能性：
- 唤醒事件尚未产生（例如 `fire_at` 尚未到达的计时器，或尚未完成的子任务）。
- 唤醒事件已产生但不匹配挂起任务的 `WakeCondition`（身份字段上的投影不匹配）。
- 没有工作者在排空队列。

**解决方案：**
1. 检查唤醒事件是否存在：对于计时器，验证 `fire_at` 已在过去；对于子任务，验证子任务已到达终态。
2. 检查任务的原始 trace——任务在等待尚未发生的事情属于按设计工作。
3. 确保有 `WorkerLoop` 在排空 dispatcher（参见[部署工作者](../how-to/deploy-worker.md)）——没有东西为你启动它。

## Provider 返回 401 / 认证错误

**症状：** 轮次因来自 LLM 端点的认证或权限错误而失败。

**原因：** API 密钥缺失、过期或无权访问请求的模型。

**解决方案：**
- 验证传给 provider 适配器的密钥。
- 如果使用企业代理，在环境中设置 `HTTPS_PROXY`。

## "Model not found" 或 provider 错误

**症状：** provider 返回模型未找到或未知模型错误。

**原因：** 你传给 `query` / `Client` 的 `model` 不是端点提供的 id。

**解决方案：**
- 让 `model` 精确对应你的端点实际提供的模型名。
- 厂商命名陷阱：Anthropic 模型名称包含日期后缀（`claude-sonnet-4-5-20250929`）；检查你的密钥的访问层级。
- SDK 目录不认识的模型，需要补上 `context_window` / `max_output_tokens`，上下文 compaction 才能生效。

## WorkerLoop：关闭时步骤被放弃

**症状：** 发送 SIGTERM 后，工作者日志显示 `shutdown_abandoned` 和 `loop.abandoned = True`。

**原因：** 进行中的步骤未在 `shutdown_grace_s`（默认 30 秒）内完成。循环放弃了它。

**解决方案：**
- **退出进程。** Python 无法中断被放弃的步骤线程；它可能仍在写入 EventLog。放弃后不支持进程内重用。
- 进程退出后，lease 过期，`requeue_stale()` 在下一次启动时回收任务。
- 为避免这种情况，在构造 `WorkerLoop` 时增加 `shutdown_grace_s`，或将其设置为 `None` 以无限等待（然后真正卡住的步骤需要 `kill -KILL <pid>`）。

## 另见

- [已知限制](limitations.md)——非 bug 的架构边界
- [唤醒与恢复](../concepts/wake-resume.md)——唤醒机制如何工作
- [WorkerLoop 参考](../reference/worker-loop.md)——构造函数参数和关闭语义
