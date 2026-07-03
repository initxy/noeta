# 核心概念

Noeta 围绕一小组原语构建。本页逐一介绍每个原语及其协作方式。词汇的唯一真相来源是[术语表](reference/glossary.md)；架构决策位于[架构决策](adr/index.md)下。

## Task

`Task` 是唯一的原语（[ADR：任务作为唯一原语](adr/task-as-only-primitive.md)）。它是代理工作的可寻址单元——具有 `task_id`、`status`（`pending` / `running` / `suspended` / `terminal`），以及如果由另一个任务生成则有 `parent_task_id`。状态通过 fold EventLog 重建；Engine 从不跨 Engine 运行在内存中持有任务状态。

## EventLog

每任务的只追加 `EventEnvelope` 记录流（[ADR：事件溯源真相](adr/event-sourced-truth.md)）。每次状态变化都会发出一个 envelope：`TaskCreated`、`MessagesAppended`、`LLMRequestStarted`、`ToolCallStarted`、`TaskSuspended`、`TaskWoken`、`TaskCompleted` 等等。EventLog 是唯一的真相来源——不存在 Engine 读取的单独"任务表"。

实现：

* `InMemoryEventLog` —— 用于测试和内存默认（未设置 `NOETA_AGENT_SQLITE`）
* `SqliteEventLog` —— 持久化的 WAL 模式 sqlite3 文件

两者都实现相同的 `EventLog` Protocol（[ADR：存储协议 L0](adr/storage-protocols-l0.md)）。

## ContentStore

内容寻址、按哈希去重的 blob 存储（[ADR：事件溯源真相](adr/event-sourced-truth.md)）。大于 4 KB 事件载荷上限的主体上传到此处；envelope 仅携带 `ContentRef(hash, size, media_type)`。示例：完整的 LLM 请求/响应主体、大型工具输出。

## Dispatcher

拥有每段 Worker 租约模型（[ADR：Worker 租约模型](adr/worker-lease-model.md)）。Worker 调用 `enqueue → lease → (heartbeat*) → release / fail` 来驱动就绪任务；`wake` 将挂起的任务重新入队。Dispatcher 还充当 EventLog 每次 `emit(lease_id=…)` 时查询的 `LeaseRegistry`，以强制执行[单写者不变量](adr/single-writer-invariant.md)。

## Engine

无状态步进驱动器。`run_one_step(task, lease_id=…)` 通过一次 Policy 决策推进任务：它组合上下文、运行 Guards、向 Policy 请求 `Decision`、应用 Decision 的效果（工具调用、LLM 往返、子任务生成、挂起、终止），并发出 envelopes。[ADR：Guard-Observer 钩子](adr/guard-observer-hooks.md) 将 Engine 类主体限制在 500 行以内，以保持可读性。

## Policy

从折叠的任务视图返回类型化的 `Decision`：`ToolCallsDecision`、`FinishDecision`、`FailDecision`、`SpawnSubtaskDecision`、`WaitTimerDecision`、`YieldForHumanDecision`。ReAct policy 是生产 policy；stub policies（`StubFinishPolicy`、`StubScriptedPolicy`）是确定性测试替身。

## Composer

从 `RuntimeState`（折叠的）到三段上下文（stable_prefix / semi_stable / dynamic_suffix）的纯函数。Composer 在每次 `run_one_step` 中调用一次，并写入一个 `ContextPlanComposed` envelope，精确记录该步骤构建上下文所依据的内容。

## Guard / Observer

[ADR：Guard-Observer 钩子](adr/guard-observer-hooks.md) 定义了两个钩子接口。

* **Guards** 位于 Engine 的热路径上。`BudgetGuard` 和 `PermissionGuard` 内置提供。Guards 可以拒绝工具调用、拒绝子任务生成，或强制预算耗尽失败。
* **Observers** 通过 `subscribe(callback)` 订阅 EventLog。回调在每个 envelope 持久化之后*同步*运行，在写者线程上但在写者锁之外。`AuditObserver`、`MetricsObserver`、`EventFanout` 和 `ChildLifecycleObserver` 内置提供。

## 基于 Fold 的状态重建 { #fold-based-state-reconstruction }

由于 EventLog 是唯一的真相来源，任务的完整状态在每次唤醒 / SSE 重连 / 检查时从日志中**确定性地 fold**（快照加速）。这种重建是挂起/恢复和多轮对话的支柱；它只向前 fold，从不重新调用 provider。

## 唤醒-恢复 { #wake-resume }

当任务以类型化的 `WakeCondition`（`SubtaskCompleted` / `HumanResponseReceived` / `TimerFired`）挂起时，Dispatcher 通过**投影**匹配传入的唤醒事件——只有标识字段参与匹配（例如子任务的 `subtask_id`；人类响应的 `handle`；计时器的 `fire_at`，带有阈值语义 `event.fire_at >= condition.fire_at`）。

匹配的事件在下一次 `lease()` 时通过 `Lease.wake_event` 交给 Worker，并传入 `Engine.note_woken` 以在继续之前写入一个持久的 `TaskWoken(wake_event=…)` envelope。交付是**单 Worker 持久 exactly-once**（H2 / [ADR：子任务扇出与持久唤醒](adr/subtask-fanout-and-durable-wake.md)）：匹配的唤醒**在租约期间存活**，并且仅由消费性的 `release(consumed_wake_event=…)` 清除，因此 Worker 在租约和 `TaskWoken` 写入之间崩溃不会丢失它——`requeue_stale()` 将任务带回就绪状态并保留唤醒事件，下一次租约会重新交付它。消费是幂等的：Worker 的唤醒分支是一个以最新匹配的 `TaskWoken` 为键的恢复状态机，因此重新交付其 `TaskWoken` 已经落地的事件会被调和而不会发出第二个。无需操作员重新发出。当 Worker 租用一个没有排队唤醒事件的挂起任务时，它记录一个 `suspended_without_wake` 可靠性事件（任务只是在等待尚未发生的唤醒——这是诊断信息，而非丢失）。

范围是单主机 / 单 Worker；部分步骤孤立边缘（步骤中途崩溃）和多 Worker / 多主机并发仍然是限制。参见[`docs/failure-modes.md`](failure-modes.md)。

## 步骤如何流动 { #how-a-step-flows }

1. Worker 调用 `dispatcher.lease(...)` 并获得 `Lease(lease_id, task_id, expires_at, wake_event?)`。排空循环是库原语 `noeta.runtime.worker.WorkerLoop`（随附的操作员 CLI worker 在 TL6 中已移除；嵌入者在进程内运行 `WorkerLoop(...).run_forever(...)`）。
2. Worker 将 EventLog fold 为 `RuntimeState`。
3. 如果设置了 `lease.wake_event`，Worker 调用 `engine.note_woken(task, lease_id, wake_event=...)`，它写入 `TaskWoken`。
4. Worker 调用 `engine.run_one_step(task, lease_id=...)`。Engine：
   * 运行 Composer → `ContextPlanComposed`
   * 调用已注册的 Guards（`pre_decide` / `pre_tool_call`）
   * 向 Policy 请求 `Decision`
   * 按 Decision 类型分派——每个处理器通过租约验证的 EventLog 写入其 envelopes
5. Worker 调用 `dispatcher.release(lease_id, next_state=…, wake_on=…)`（或 `dispatcher.fail(...)`）。
6. Observers 在每次成功的 `emit` 后看到 envelopes。它们在写者线程上同步运行但在写者锁之外，因此任何 Observer 异常都会被吞没。
