# Engine

**Engine** 负责推进一个 Task。它只有一个动作 `run_one_step`：把 Task 一直跑到需要等待或结束——一整轮，不管中间调了几次模型、几次工具——然后返回。两次调用之间它什么都不记，每次都从日志重新 fold 出的状态开始。

<NtEngineLoop lang="zh" />

## 保证什么

- **做决定和写记录分开。** policy 只返回决策，从不写日志；每个结果都由 Engine 记下来。policy 写得再糟也改不坏记录。
- **guard 能拦，observer 不能。** guard 在动作执行前运行，可以阻止它；observer 只能在事件落盘后看到它，什么也改不了。
- **guard 出错就拒绝。** guard 抛异常，这次调用就被拒绝；observer 抛异常会被忽略，Task 照常运行。
- **长步骤也有恢复点。** 每连续 20 轮工具调用写一次快照。

## 循环

1. **组装。** 上下文组装器拼出 `View`——模型这次要看到的完整 prompt、工具定义和消息——Engine 记一条 `ContextPlanComposed` 事件。见[上下文与缓存](context.md)。
2. **决策。** **policy** 读 `View`，返回一个带类型的 `Decision`。默认是 `ReActPolicy`，由它去问模型。
3. **执行。** Engine 执行这个决策——跑工具、派子任务、挂起、结束——每个结果都在 worker 的租约下追加到日志。

下表里所有「继续」类的决策（不只是工具调用）都会直接回到「组装」；只有挂起或结束才会让这次调用返回。用 `Client` 时，一轮通常以挂起收尾：Task 等你的下一条消息。只有 `query()`（或 `multi_turn=False`）才会以 `TaskCompleted` 结束 Task。

| 走向 | 决策 | 结果 |
| --- | --- | --- |
| 继续 | `ToolCallsDecision`、`StatePatchDecision`、`CompactionRequestedDecision`、后台的 `SpawnSubtaskDecision` | 记事件，再循环一次 |
| 挂起 | 前台的 `SpawnSubtaskDecision`、`SpawnSubtasksDecision`、`YieldForHumanDecision`、`WaitTimerDecision`、`WaitExternalDecision` | 写快照、`TaskSuspended`、释放租约 |
| 结束 | `FinishDecision`、`FailDecision` | 写快照和结束事件 |

这些决策不对应任何具体产品功能：更新待办是一次状态修改，问用户是 `YieldForHumanDecision`。由提供这个工具的内置插件负责翻译。

## 从宿主看一轮

你的代码把任务交出去，某个 worker 拿到租约、从日志重建状态。从拿到租约到释放租约，中间就是一次 `run_one_step`。取消是协作式的：Engine 在每一圈开头和 policy 刚做完决定时检查是否被取消，所以取消会在下一个轮次边界生效。

## guard 和 observer

| | guard | observer |
| --- | --- | --- |
| 何时运行 | 动作执行前，同步 | 事件落盘之后 |
| 能否阻止 | 能：放行 `allow`、拒绝 `deny`、要审批 `require_approval` | 不能 |
| 抛异常时 | 动作被拒绝 | 异常被吞掉 |
| 适合做 | 权限、预算、打断死循环 | 审计、指标、链路追踪、推送给 UI |

guard 检查三类动作：`ProposedToolCall`、`ProposedSpawnSubtask`、`ProposedFinish`。按 `priority` 从小到大依次运行，**第一个不放行的结论算数**，所以排在后面的 guard 只能在前面放行的基础上收紧。`governance` 内置插件默认装上：

| 优先级 | guard | 管什么 |
| --- | --- | --- |
| 10 | `BudgetGuard` | 轮数、工具调用次数、花费、子任务数和深度上限 |
| 20 | `PermissionGuard` | 工具和 agent 白名单、风险等级上限 |
| 30 | `RepetitionGuard` | 打断一连串完全相同的工具调用 |
| 100 | `HookGuard` | 你配置的 PreToolUse 规则 |

`require_approval`（要审批）挂起 Task 的方式和问用户问题完全一样，所以审批走的是普通唤醒流程。

observer 在每次追加提交后被调用，不在写锁里，可能同时被多个线程调用——自己的状态要自己加锁。唯一会写东西的 observer 是 `ChildLifecycleObserver`，它只往*父任务*的日志追加一条 `SubtaskCompleted`；任何一条日志都不会多出第二个写入方。

## 对你意味着什么

- 想改变 *agent 怎么决定*，换 policy；想*拦住*某个动作，写 guard；只想*看*，写 observer。没有别的钩子。
- 自己的可以这样接：`Options(guards=(MyGuard(),), observers=(fn,))`，都不影响 agent 身份。插件带来的 guard 或 observer 一旦加载，就对进程里所有 agent 生效——管控不能被跳过。
- Engine 的主循环本身不开放扩展，它周围的一切都可以换。

设计记录：
[guard and observer hooks](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md) ·
[engine per turn](https://github.com/initxy/noeta/blob/main/docs/adr/engine-per-turn.md)

## 下一步

- [上下文与缓存](context.md)：「组装」这一步拼出了什么。
- [任务与唤醒](tasks-and-waking.md)：挂起之后发生什么。
- [写插件](../guides/plugins.md)：把 guard 或 observer 打包成插件。
