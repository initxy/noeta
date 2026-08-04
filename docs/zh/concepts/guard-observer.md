# Guard 与 Observer

在 Noeta 里，挂接到一个运行中代理上的方式恰好有两种，而在它们之间做选择只需要问一个问题：**你的钩子需要阻止这个动作，还是只需要看到它？**

如果它需要阻止某件事——一次不被允许的工具调用、一份已经花光的预算——你要的是 **Guard**。如果它只需要知道某件事发生过——审计、指标、追踪、把更新推给 UI——你要的是 **Observer**。没有第三种选项，而这是刻意的。

## 一眼看清

| | Guard | Observer |
| --- | --- | --- |
| 运行时机 | 效果之前，同步 | 事件被持久追加之后 |
| 能否否决 | 能——`ALLOW` / `DENY` / `REQUIRE_APPROVAL` | 不能 |
| 抛异常时 | 视为一次 deny——fail-closed | 被吞没；Task 不受影响 |
| 典型用途 | 权限、预算、打断循环 | 审计、指标、追踪、扇出 |
| 作用范围 | 加载后即进程范围 | 加载后即进程范围 |

## Guard：热路径上的同步否决

一个 Guard 在 Engine 的这一步内部运行，在效果发生*之前*，作用于三个动作点：`ProposedToolCall`、`ProposedSpawnSubtask`、`ProposedFinish`。因为它在效果之前完成，所以能真正阻止效果。

接口很小：

```python
class Guard(Protocol):
    name: str
    priority: int

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult: ...
```

它返回一个 `VerdictResult`，携带三种裁决之一以及一个可选的理由：

```python
VerdictResult.deny("Bash is disabled for this agent")
```

`REQUIRE_APPROVAL` 不会开启一条平行的生命周期。Engine 把它映射到 `YieldForHumanDecision` 走的那个人类挂起出口，因此审批复用同一个唤醒句柄和同一条恢复分支（见[唤醒与恢复](wake-resume.md)）。

### 一个 Guard 能看到什么

`GuardContext` 是只读的：`task_id`、fold 出的 `GovernanceState` 的一份 deepcopy、Task 的 `active_skills` 与 `subtask_depth`、最近的工具调用身份键，以及一个自由形式的 `metadata` 袋子。deepcopy 是关键——一个改动了传给它的数据的 Guard 无法扰动 Engine 状态。

### 这个栈如何裁决

`HookManager`（`noeta.core.hooks`）按 `priority` 升序运行已注册的 Guard，并返回**第一个非 allow** 的裁决。优先级更低的 Guard 之后根本不会被咨询。

一个 `check` 抛出异常的 Guard 会被转换成一个指名该 Guard 的 `DENY`，并由这个 deny 决定结果。一个损坏的 Guard 绝不会悄悄放行它本就为了拦住而存在的东西。

`governance` built-in 贡献了默认的栈：

| 优先级 | Guard | 强制执行 |
| --- | --- | --- |
| 10 | `BudgetGuard` | 迭代次数、工具调用、成本、派生的子任务、子任务深度的上限 |
| 20 | `PermissionGuard` | 工具 / agent 白名单和风险等级上限，fail-closed |
| 30 | `RepetitionGuard` | 打断卡在相同 `(tool_name, arguments)` 调用上的运行 |
| 100 | `HookGuard` | 用户配置的 PreToolUse 规则 |

因为第一个非 allow 获胜，优先级 100 的用户规则只能收紧内置项已经允许的调用。它既不能放松一次内置的拒绝，也不能改写一次内置的批准。

## Observer：提交后的订阅者

一个 Observer 通过 `EventLogSubscriber.subscribe` 把一个回调订阅到 EventLog。每个存储后端都遵守同一个投递契约：

- 回调在 append 持久化**之后**、发起该次 `emit` 的调用返回之前触发；
- 它在后端的写者锁**之外**触发，因此多个写者线程可以并发进入同一个回调——每个 Observer 各自用锁守护自己的状态；
- 它抛出的异常会被**吞没**，因此一个损坏的 Observer 不会把一个 Task 一起拖垮。

仓库内的 Observer 有 `AuditObserver`（把每个信封按白名单投影到一个 sink——从不包含负载正文）、`MetricsObserver`（按类型和按 Task 的计数器）、`TraceExportObserver`（把那个投影发往一个外部 sink）、`EventFanout`（与传输无关地扇出到每个消费者有界的队列），以及 `ChildLifecycleObserver`（父 ↔ 子的交接）。`governance` built-in 为用户的 PostToolUse / Notification 钩子增加了 `HookObserver`；它把工作入队到一个后台线程，而不是在 emit 路径内运行子进程。

### 唯一一个会写的 Observer

Observer 只读。`ChildLifecycleObserver` 是例外，而且它写得很窄：它通过 `system_emit` 向**父任务的**流追加一个 `SubtaskCompleted`——一次无租约的、标记为 `origin="observer"` 的跨流 append——并把唤醒交给 Dispatcher。

它从不写入触发它的那个事件所在的流。这正是这个例外之所以安全的原因：没有任何一条流会因此获得第二个并发写者。

## 为什么要分开，以及为什么没有第三种角色

否决位于热路径上，因此它的 Surface 被限制在三个明确定义的点上，而它的失败必须响亮。观察绝不能阻塞或破坏执行，因此它被推到提交之后，并被剥夺了大声失败的权利。

把两者合并成一个"中间件" Surface，会迫使每个审计钩子都像权限检查一样被信任——而且会招来那些在放行路上改写负载的钩子。一个想改变*发生什么*的钩子，必须转而成为某个 Policy 或 ContextComposer 的一部分；单写者不变式不容纳第二个写者（见[事件溯源](event-sourcing.md)）。

## 接上你自己的

两者都是进程范围的接线 Surface，永不冲突，因此每个贡献都会生效：

```python
Options(
    system_prompt="…",
    guards=(MyGuard(),),                 # registered after the built-in stack
    observers=(lambda envelope: ...,),   # subscribed alongside the defaults
)
```

两者都不进入 agent 身份——两个仅在 guard 或 observer 上不同的配方，编译成相同的 `AgentSpec`。而且因为治理属于运维方的权限、而非按 agent 的配置，一个被加载的 Guard 或 Observer 在整个进程范围内生效，而不是跟随插件的激活。

## 下一步

- [引擎与执行](engine-execution.md) —— Guard 在其中运行的那一步。
- [编写插件](../how-to/write-a-plugin.md) —— 把一个 Guard 或 Observer 打包成一个贡献。
- [插件 Surface](../reference/plugin-surfaces.md) —— `guard` 和 `observer` 两个 Surface 的完整说明。
- [SDK Options](../reference/sdk-options.md) —— `guards` / `observers` 字段和各个权限模式。
