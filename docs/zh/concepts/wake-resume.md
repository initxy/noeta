# 唤醒与恢复

一个正在等待的 Task 不会阻塞线程——它**挂起**。挂起是一个状态，附带一个类型化的 `WakeCondition`，无论等待的原因是什么：`SubtaskCompleted`（派生子任务完成）、`HumanResponseReceived`（回答或批准），或 `TimerFired`（定时唤醒）。Task 的状态安全地存放在它的 EventLog 中；等待期间，进程内存中不保留任何关于它的东西（见[任务模型](task-model.md)）。

## 唤醒如何匹配

当一个唤醒事件到达时，Dispatcher 通过**投影**将其与挂起的 Task 匹配：只有标识字段参与匹配——子任务的 `subtask_id`、人类响应的 `handle`、定时器的 `fire_at`（使用阈值语义，即 `event.fire_at >= condition.fire_at`）。匹配成功后将 Task 重新入队；下一个租用它的 Worker 会在收到租约的同时收到唤醒事件，Engine 在 Task 继续之前写入一个持久的 `TaskWoken` 信封。恢复随后就是一次 fold——不存在单独的恢复路径（见 [Fold & 快照](fold-and-snapshot.md)）。

## 投递保证

投递是**单 Worker 持久恰好一次**。匹配到的唤醒由 Dispatcher 持久持有，其生命周期超越任何单个租约：只有当某个步骤消费了它时才清除，这发生在 `TaskWoken` 信封安全写入日志之后。如果 Worker 在租用后、写入前崩溃，过期租约清扫会将 Task 连同其完好的唤醒事件一起送回就绪队列，下一次租约会再次投递同一个唤醒。重复投递是幂等的：如果 `TaskWoken` 信封已经落地，Worker 会与之对账，而不是写入第二个。无论哪种情况都不需要人工干预——唤醒会自动、持久地触发一次。

一个挂起但没有排队唤醒的 Task 不是错误：它只是在等待尚未发生的事情。检查这样的 Task 会报告一个类型化的 `suspended_without_wake_event`——这是一个诊断信息，不是失败。（这一保证背后的完整崩溃恢复机制在[架构概览](../architecture/overview.md)中有描述。）

## "单主机 / 单 Worker"是什么意思

上述保证的范围限于已交付的部署形态：一个持久存储（SQLite）和一个常驻 Worker 进程从中消费。在这个范围内，Worker 在匹配和消费之间任何时刻的崩溃都会解析为恰好一个持久的 `TaskWoken`。**步骤中途**的崩溃——在 `TaskWoken` 之后、该步骤的其余事件落地之前——会在下一次租约时恢复：被中断的尝试会被一个持久的 `StepAttemptAbandoned` 标记封存，如果它没有记录任何有副作用的活动，则自动重新驱动；否则 Task 会被停放为一个已停止的对话，并附带一条系统通知供人类核实。一个边界仍然开放：多 Worker / 多主机并发（竞争 Worker 之间的 fencing 尚未交付）。恢复范围和该边界都在[已知限制](../operations/limitations.md)中有记录。

相关：[任务模型](task-model.md) ·
[引擎与执行](engine-execution.md) ·
[Fold & 快照](fold-and-snapshot.md)
