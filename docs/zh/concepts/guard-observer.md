# Guard 与 Observer

Noeta 恰好有两个钩子面，由一个问题划分：**钩子需要阻止动作，还是只需要看到它？**

## Guard：热路径上的同步否决

一个 Guard 在 Engine 的步骤内部运行，在效果发生*之前*，有三个时机：工具调用之前、子任务派生之前、完成之前。它的裁决是 `allow`、`deny` 或 `require_approval`。因为 Guard 在效果之前完成，它能真正阻止效果——拒绝一个 shell 命令、阻止工作区外的写入，或强制触发预算耗尽失败。

两个 Guard 随仓库内置提供：

- **`BudgetGuard`** —— 强制执行 Task 的资源上限（迭代次数、成本、墙钟时间、工具调用次数）。
- **`PermissionGuard`** —— 实现 `permission_mode` 背后的权限模型（高风险工具是否必须在运行前请求许可）。

## Observer：事后的只读订阅者

一个 Observer 通过 `subscribe(callback)` 订阅 EventLog。回调在每个信封持久化*之后*运行——在写者线程上但在写者锁之外——并且是严格只读的：Observer 不能写入事件，因此单写者不变式成立（见[事件溯源](event-sourcing.md)）。Observer 的异常会被吞没；一个损坏的 Observer 永远不会把 Task 拖垮。

内置 Observer：`AuditObserver`、`MetricsObserver`、`EventFanout`（Web UI 背后的 SSE 流），以及 `ChildLifecycleObserver`。

## 为什么要分开

| | Guard | Observer |
| --- | --- | --- |
| 运行时机 | 效果之前，同步 | 信封持久化之后 |
| 能否否决 | 能（`allow` / `deny` / `require_approval`） | 不能——只读 |
| 能否写入状态 | 不能 | 不能 |
| 失败影响 | 一次 deny 是一个被记录的结果 | 异常被吞没；Task 不受影响 |
| 典型用途 | 权限、预算 | 审计、指标、实时流 |

否决必须是同步且罕见的——它位于热路径上，所以钩子面被限制在三个明确定义的时机。观察绝不能阻塞或破坏执行——所以它被推到提交之后，并被剥夺写入权限。将两者合并为一个"中间件"面会迫使每个审计钩子都像权限检查一样被信任；将它们分开意味着扩展一个不会削弱另一个。

两个面都是开放的扩展点：通过 `Options` 传入你自己的 `guards` 和 `observers`（完整扩展面见[架构概览](../architecture/overview.md)）。

相关：[引擎与执行](engine-execution.md) ·
[事件溯源](event-sourcing.md)
