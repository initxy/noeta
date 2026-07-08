# 故障排查

常见问题及其解决方法。每条目遵循**症状 → 原因 → 解决方案**。

## 服务器启动时退出："needs NOETA_AGENT_API_KEY"

**症状：** `python -m noeta.agent` 打印 `NOETA_AGENT_PROVIDER='openai' needs NOETA_AGENT_API_KEY` 并退出。

**原因：** 你将 `NOETA_AGENT_PROVIDER` 设置为真实 provider（`openai`、`anthropic`、`openai-responses`）但未提供凭据。

**解决方案：**
- 在环境中设置 `NOETA_AGENT_API_KEY=sk-…`。
- 对于 `openai`，还需设置 `NOETA_AGENT_BASE_URL=https://api.openai.com/v1`（或你的 OpenAI 兼容端点）。
- 或使用 `NOETA_AGENT_PROVIDER=stub`（默认值）进行完全离线的冒烟测试——不需要密钥。

## 任务失败并报 "max_iterations exceeded"

**症状：** 会话以预算拒绝原因终止，如 `"max_iterations=5 exceeded"` 或 `"max_tool_calls=3 reached"`。

**原因：** `BudgetGuard` 拒绝了下一个动作，因为超过了配置的预算轴（迭代、工具调用、成本、生成的子任务）。任务仍然运行并产生了持久化信封——只是以不成功的方式终止。

**解决方案：**
1. 检查 trace 以查看哪个预算轴触发了，以及任务为什么需要这么多步骤。
2. 提高预算：编码代理的默认值位于 `noeta.agent.host.session.default_coding_budget()`。编程调用者通过 `Options.budget` 传递 `BudgetSpec`。
3. 或收窄任务范围以减少所需步骤。

## 工具调用被 PermissionGuard 拒绝

**症状：** 代理尝试使用工具并收到 `ToolCallDenied` 事件。trace 显示拒绝原因。

**原因：** `PermissionGuard` 拒绝了工具调用，因为该工具不在代理的 `allowed_tools` 集合中，或 `permission_mode` 要求对该风险级别进行显式批准。

**解决方案：**
- 在你的 `Options` 中扩大 `allowed_tools` 以包含该工具。
- 或将 `permission_mode` 更改为 `"bypassPermissions"` 以用于低风险工具（不推荐用于 `edit`、`write` 或 `shell_run`）。
- 或者，如果使用 Web UI，点击待处理审批提示上的**批准**。

## 挂起的任务永远不会唤醒

**症状：** 任务处于 `suspended` 状态但永远不会转换为 `running`，尽管它等待的条件似乎已经满足。

**原因：** 几种可能性：
- 唤醒事件尚未产生（例如 `fire_at` 尚未到达的计时器，或尚未完成的子任务）。
- 唤醒事件已产生但不匹配挂起任务的 `WakeCondition`（身份字段上的投影不匹配）。
- 工作者未运行（`WorkerLoop` 未排空队列）。

**解决方案：**
1. 检查唤醒事件是否存在：对于计时器，验证 `fire_at` 已在过去；对于子任务，验证子任务已到达终态。
2. 检查任务的 fold 详情（`GET /tasks/{id}`）——查找 `suspended_without_wake_event` 诊断。如果存在，任务只是在等待尚未发生的事情。
3. 确保 `WorkerLoop` 正在运行并排空 dispatcher。`python -m noeta.agent` 服务器**不**运行 `WorkerLoop`——它内联驱动轮次。如果你在外部入队了唤醒事件，你需要一个工作者来拾取它。参见[部署工作者](../how-to/deploy-worker.md)。

## Provider 返回 401 / 认证错误

**症状：** 代理因来自 LLM provider 的认证或权限错误而失败。

**原因：** API 密钥缺失、过期或无权访问请求的模型。

**解决方案：**
- 验证 `NOETA_AGENT_API_KEY` 已设置且正确。
- 对于 Anthropic，密钥以 `sk-ant-` 开头；对于 OpenAI，以 `sk-` 开头。
- 检查模型名称——某些模型需要特定的访问或权限。
- 如果使用企业代理，在环境中设置 `HTTPS_PROXY`。

## "Model not found" 或 provider 错误

**症状：** provider 返回模型未找到或未知模型错误。

**原因：** 模型名称错误或 provider 不识别它。

**解决方案：**
- Anthropic 模型名称包含日期后缀：`claude-sonnet-4-5-20250929`，而非 `claude-sonnet`。
- OpenAI 模型名称：`gpt-5.5`、`gpt-4o` 等。
- 验证该模型在你的 API 密钥层级中可用。

## Shell 命令被允许列表拒绝

**症状：** `shell_run` 返回拒绝，尽管 `NOETA_AGENT_SHELL_MODE=allowlist`。

**原因：** 该命令不在结构化允许列表中。默认仅允许 `git status`、`git diff`、`pytest`、`uv run pytest`、`npm test` 和 `pnpm test`。Shell 元字符（管道、重定向）在分词之前被拒绝。

**解决方案：**
- 重构命令以匹配允许列表的形式。
- 或设置 `NOETA_AGENT_SHELL_MODE=off` 以完全禁用 `shell_run`（比扩大允许列表更安全）。
- 允许列表是结构化的（基于 argv 模式），而非基于字符串的——你不能通过环境变量添加自定义命令。要扩展它，请在代码中修改允许列表。

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
