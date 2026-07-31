# Guard 与 Observer

Noeta 恰好有两种钩子角色，由一个问题划分：**钩子需要阻止动作，还是只需要看到它？**

## Guard：热路径上的同步否决

一个 Guard 在 Engine 的 Step 内部运行，在效果发生*之前*，作用于三个动作点——`ProposedToolCall`、`ProposedSpawnSubtask`、`ProposedFinish`。它返回一个 `VerdictResult`，携带三种裁决之一（`ALLOW`、`DENY`、`REQUIRE_APPROVAL`）以及一个可选的理由。因为它在效果之前完成，所以能真正阻止效果。

`REQUIRE_APPROVAL` 不会开启一条平行的生命周期：Engine 把它映射到与 `YieldForHumanDecision` 相同的人类挂起出口，因此审批复用同一个唤醒句柄和同一条恢复分支。

接口很小：

```python
class Guard(Protocol):
    name: str
    priority: int

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult: ...
```

`GuardContext` 是只读的：`task_id`、fold 出的 `GovernanceState` 的一份 deepcopy、Task 的 `active_skills` 与 `subtask_depth`，以及最近的工具调用身份键。一个改动了传给它的数据的 Guard 无法扰动 Engine 状态。

`HookManager`（`noeta.core.hooks`）按 `priority` 升序运行已注册的 Guard，并返回**第一个非 allow** 的裁决；优先级更低的 Guard 不会被咨询。一个 `check` 抛出异常的 Guard 会被转换成一个指名该 Guard 的 `DENY`，并由这个 deny 决定结果——一个损坏的 Guard 不会悄悄放行它本应阻止的东西。

`governance` built-in 贡献了默认的栈：

| 优先级 | Guard | 强制执行 |
| --- | --- | --- |
| 10 | `BudgetGuard` | 迭代次数、工具调用、成本、派生的子任务、子任务深度的上限 |
| 20 | `PermissionGuard` | 工具 / agent 白名单和风险等级上限，fail-closed |
| 30 | `RepetitionGuard` | 打断卡在相同 `(tool_name, arguments)` 调用上的运行 |
| 100 | `HookGuard` | 用户配置的 PreToolUse 规则 |

因为第一个非 allow 获胜，优先级 100 的用户规则只能收紧内置项已经允许的调用；它既不能放松内置的拒绝，也不能改写内置的批准。

## Observer：提交后的订阅者

一个 Observer 通过 `EventLogSubscriber.subscribe` 把一个回调订阅到 EventLog。每个适配器都遵守同一个投递契约：回调在 append 持久化**之后**、发起该次 `emit` 的调用返回之前触发；它在适配器的写者锁**之外**触发，因此多个写者线程可以并发进入同一个回调，每个 Observer 各自守护自己的状态；而它抛出的异常会被**吞没**，因此一个损坏的 Observer 不会把一个 Task 一起拖垮。

内置的 Observer：`AuditObserver`（把每个信封按白名单投影到一个 sink——从不包含 payload 主体）、`MetricsObserver`（按类型和按 Task 的计数器）、`TraceExportObserver`（把该投影发往一个外部 sink）、`EventFanout`（与传输无关地扇出到每个消费者有界的队列），以及 `ChildLifecycleObserver`（父 ↔ 子的交接）。`governance` built-in 为用户的 PostToolUse / Notification 钩子增加了 `HookObserver`；它把工作入队到一个后台线程，而不是在 emit 路径内运行子进程。

Observer 只读。唯一会写的是 `ChildLifecycleObserver`，而且它写得很窄：它通过 `system_emit` 向**父任务的**流追加一个 `SubtaskCompleted`——一次无租约的、标记为 `origin="observer"` 的跨流 append——并把唤醒交给 Dispatcher。它从不写入触发它的那条事件所在的流，因此没有任何一条流会获得第二个并发写者。

不存在第三种角色。一个想要改写 payload 或某个状态切片的钩子必须成为 Policy 或 ContextComposer 的一部分；单写者不变式不容纳第二个写者（见[事件溯源](event-sourcing.md)）。

## 为什么要分开

| | Guard | Observer |
| --- | --- | --- |
| 运行时机 | 效果之前，同步 | 信封持久化之后 |
| 能否否决 | 能（`ALLOW` / `DENY` / `REQUIRE_APPROVAL`） | 不能 |
| 失败影响 | 视为一次 deny——fail-closed | 被吞没；Task 不受影响 |
| 典型用途 | 权限、预算、打断循环 | 审计、指标、追踪、扇出 |

否决位于热路径上，所以这个 Surface 被限制在三个明确定义的点上。观察绝不能阻塞或破坏执行，所以它被推到提交之后，并被剥夺大声失败的权利。将两者合并为一个"中间件"Surface 会迫使每个审计钩子都像权限检查一样被信任。

两者都是进程范围的接线 Surface，永不冲突，因此每个贡献都会生效。通过 `Options.guards`（`Guard` 实例，注册在内置栈之后）和 `Options.observers`（`Callable[[EventEnvelope], None]`，与默认项一起订阅，并在关闭时拆除）传入你自己的。两者都不进入 agent 身份。

相关：[引擎与执行](engine-execution.md) ·
[事件溯源](event-sourcing.md)
