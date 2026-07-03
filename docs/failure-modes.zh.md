# 故障模式 { #failure-modes }

常见故障及如何恢复。

## 缺少 API key { #missing-api-key }

配置了 `NOETA_AGENT_PROVIDER=openai`（或 `anthropic`）但没有凭据的 `python -m noeta.agent` 服务器将在启动时退出并显示：

```text
NOETA_AGENT_PROVIDER='openai' needs NOETA_AGENT_API_KEY
```

通过在环境中设置 `NOETA_AGENT_API_KEY`（或在 `NOETA_AGENT_CONFIG` JSON 文件的 `api_key` 下）来恢复。OpenAI 另外需要 `NOETA_AGENT_BASE_URL`（否则启动退出并显示 `NOETA_AGENT_PROVIDER='openai' needs NOETA_AGENT_BASE_URL`）；Anthropic 使用相同的 `NOETA_AGENT_API_KEY` 并将 `NOETA_AGENT_BASE_URL` 视为可选。

默认的 `NOETA_AGENT_PROVIDER=stub` 绕过此要求，当你只想测试接线时是正确选择。

## 预算耗尽 { #budget-exhaustion }

当任何配置的预算轴被跨越时——迭代次数、工具调用次数、成本 USD、生成的子任务数——`BudgetGuard` 拒绝一个 `ProposedAction`（工具调用、子任务生成或完成）。guard 返回 `VerdictResult.deny(reason=...)`，带有逐轴原因字符串，如 `"max_iterations=5 exceeded"` 或 `"max_tool_calls=3 reached"`。根据被提议的操作，Engine 发出 guard-denial envelope（`ToolCallDenied` / `SubtaskDenied`），或者当迭代或成本上限在任何允许的操作剩余之前触发时，发出 `TaskFailed` envelope。确切原因是 BudgetGuard 返回的任何字符串——没有固定的 `budget_exhausted_*` 分类法。

预算耗尽的任务仍然运行并产生了持久 envelopes；它只是不成功地终止了。恢复终止的任务返回类型化的 `reason: terminal` 失败。

恢复方法：

* 检查记录（EventLog 读取模型 / 代码会话的 inspect projection）以在相关拒绝 / `TaskFailed` envelope 中读取确切的拒绝原因
* 提高预算——预算是代理 `Options`（身份）的一部分；编程 SDK 调用者通过 `Options` 传递 `Budget(...)`（或 `BudgetSpec` 字段），未设置的上限委托给 `Budget()` 默认值，`python -m noeta.agent` 从主机配置 / `main` 预设接线它。（`noeta.testing.profile.default_budget()` helper 仅用于测试/演示默认值——生产永远不会导入 `noeta.testing`。）
* 修剪任务范围以减少所需步骤

## 权限拒绝 { #permission-denial }

`PermissionGuard` 拒绝请求被拒绝工具或代理的 `ToolCallsDecision` 或 `SpawnSubtaskDecision`。Engine 发出 `ToolCallDenied` / `SubtaskDenied` envelopes；策略在下一轮决策中看到拒绝。

通过扩大权限策略（`PermissionPolicy.allowed_tools` / `allowed_subtask_agents`）或更改任务目标以避免被拒绝的操作来恢复。

## 持久恰好一次唤醒（H2） { #durable-exactly-once-wake-h2 }

当通过 `dispatcher.wake(...)` 唤醒挂起的任务时，匹配的事件存在于 dispatcher 行上。**H2（[ADR：子任务扇出和持久唤醒](adr/subtask-fanout-and-durable-wake.md)）使唤醒传递和消费在崩溃后恰好一次**（单主机 / 单 worker）：匹配的唤醒**在 `lease()` 中存活**（它不再在租约时被销毁），仅由呈现已消费唤醒的**消费释放**清除（在持久 `TaskWoken` 写入之后），否则在崩溃后由 `requeue_stale()` **重新传递**。因此，在 `lease()` 和 `TaskWoken` 写入之间的 worker 崩溃不再丢失唤醒——`requeue_stale()` 将任务带回就绪状态**并保留唤醒**，下一个租约重新传递它。

消费是幂等的：worker 的唤醒分支是一个恢复状态机，以当前 suspend-window 内最新的匹配 `TaskWoken` 为键。已经写入了 `TaskWoken` 的重新传递被协调（终止 / 重新挂起 / 继续）**而不发出第二个 `TaskWoken`**；尚未写入 `TaskWoken` 的重新传递恰好发出第一个。净效果：*应该触发的唤醒总是触发；重新传递的唤醒仅被消费一次。***不需要操作员重新发出**（以前的手动 `dispatcher.wake(...)` 恢复配方已过时）。

范围：单主机 / 单 worker。多 worker 并发（并发重新传递、fencing、完成排序）是未来的切片。在**飞行中途**崩溃的步骤——在 `TaskWoken` 之后，有部分步骤事件，仍然 `running`——是下面的 **partial-step-orphan** 限制：H2 不会静默地重新运行部分步骤（worker 引发类型化的 `PartialStepOrphan`）。

## 常驻 worker 循环（`WorkerLoop`） { #resident-worker-loop-workerloop }

单主机常驻排空循环不再是 shell 命令——`noeta serve` 在 TL6 中被移除。排空循环现在是**库原语** `noeta.runtime.worker.WorkerLoop`：嵌入器构造它并调用 `WorkerLoop(rt, ...).run_forever(install_signals=True)`（`install_signals=True` 标志通过 `noeta.runtime.worker.install_stop_signals` 将 SIGTERM/SIGINT 接线到 `loop.stop()`）。Noeta 发布的任何东西都不会启动它。`noeta serve` 曾经兼作的聊天服务器 / UI 用例现在是 `python -m noeta.agent`（环境配置的 HTTP/SSE 服务器 + 捆绑 SPA——它启动**无** `WorkerLoop`；委派关闭）。见[`daemon.md`](daemon.md) 了解循环的模型和完整限制列表。

嵌入器遇到的两个恢复路径：

**循环下的唤醒（持久恰好一次，H2）。** 在租赁唤醒任务和写入 `TaskWoken` 之间的 worker 崩溃不再丢失唤醒：匹配的唤醒在租约中存活，`requeue_stale()` 重新传递它，消费是幂等的（见上文[持久恰好一次唤醒（H2）](#durable-exactly-once-wake-h2)）。不需要操作员重新发出。单主机 / 单 worker 范围；多 worker 是未来的切片。

**卡住的步骤。** 关闭是**有界进程关闭**（H1）：SIGTERM/SIGINT 翻转 `loop.stop()`（循环在当前同步步骤完成后的下一次迭代顶部注意到），然后 `run_forever` 等待循环的 `shutdown_grace_s` 以等待飞行中的步骤；如果它没有完成，循环**放弃**它（停止其心跳，发出 `shutdown_abandoned`，设置 `loop.abandoned`）而不释放或失败租约，`run_forever` 返回。主机**必须然后退出进程**——Noeta **不**中断正在运行的步骤（Python 不能杀死线程），因此放弃仅在进程退出时生效，将被放弃的守护线程带走。被放弃的租约然后过期；循环的定期 `requeue_stale()` 扫描（通过 `maybe_sweep()` 每次迭代运行，节奏 `stale_sweep_interval`）将任务返回到就绪队列——在下一个进程的循环上，因为放弃的进程退出了。（使用 `shutdown_grace_s=None` 或 `<= 0` 构造循环以选择旧的无界等待；真正挂起的步骤然后需要外部 `kill -KILL <pid>`。）

```bash
kill -TERM <pid>   # graceful: loop.stop() → finishes within grace, else
                   # abandons + the host exits the process
```

没有**持久 EventLog** 状态丢失——直到最后一个持久步骤的记录是完整的，仅租约 / `TaskStarted` 之前的崩溃恢复字节相等。（注意：留下孤儿事件的**部分步骤**崩溃——见[`daemon.md` → 崩溃恢复范围](daemon.md#crash-recovery-is-scoped-to-the-no-orphan-event-class)——是已知限制；在放弃/杀死之前产生副作用但从未完成其 EventLog 记录写入的工具/外部 API 可能在重试时重复该效果——外部效果幂等性未在此处解决。）

如果步骤反而耗尽了心跳 keepalive 窗口（`heartbeat_interval × heartbeat_max`），租约被强制释放，步骤的下一次 EventLog 写入失败并显示 `InvalidLease`。循环记录并继续。此上限命中是**操作故障信号，不是恢复路径**：循环无法区分上限命中和正常租约过期，因此 3A 对任务返回就绪或被未来租约拾取**不做任何承诺**。检查它——对运行中的 `python -m noeta.agent` 服务器的 HTTP `GET /tasks`（会话列表）和 `GET /stream?task=<id>`（envelope 重放），或 Python `noeta.core.fold.fold(event_log, content_store, task_id)`——并手动决定做什么。

## Engine 类体超出预算 { #engine-class-body-over-budget }

Engine 类体上限为 500 行（[ADR：Guard-observer 钩子](adr/guard-observer-hooks.md)）。越过该线的 PR 将失败 `test_real_engine_under_500_budget` 门。通过遵循 C3 模式将 handlers 移动到 `noeta/core/_decision_handlers.py` 来重构。
