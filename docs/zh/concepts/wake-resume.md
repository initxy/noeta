# 唤醒与恢复

一个正在等待的 Task 不会阻塞线程——它**挂起**。挂起是一个状态，在 `wake_on` 上附带一个类型化的 `WakeCondition`，无论等待的原因是什么。Task 的状态安全地存放在它的 EventLog 中；等待期间，进程内存中不保留任何关于它的东西（见[任务模型](task-model.md)）。

## 唤醒如何匹配

条件和事件共用同一个 dataclass：父任务存下它正在等待的形态，稍后由某个生产者通过 `Dispatcher.wake` 投递同一个类型。匹配通过**投影**进行——只有标识字段参与：

| 条件 | 由谁投递 | 匹配依据 |
| --- | --- | --- |
| `SubtaskCompleted` | `ChildLifecycleObserver` | `subtask_id`（子任务的 `result` 随行携带，供参考） |
| `SubtaskGroupCompleted` | `ChildLifecycleObserver` | `group_id`（成员的 `subtask_ids` 随行携带） |
| `HumanResponseReceived` | 面向人类的通道 | `handle` |
| `TimerFired` | Worker 的定时器轮询 | 阈值：`event.fire_at >= condition.fire_at` |
| `ExternalEvent` | 任意外部入口 | `event_kind` |

`matches_wake` 是这张真值表的唯一实现，每个 Dispatcher 都经由它路由，因此没有任何适配器能私下产生分歧。跨类型的匹配永远为假：一个子任务唤醒无法满足一个定时器条件，无论它的字段说什么。

一次匹配会把 Task 重新入队。下一个租用它的 Worker 在 `Lease.wake_event` 上收到这个唤醒，Engine 写入一个持久的 `TaskWoken` 信封，然后该 Step 运行。恢复随后就只是一次 fold——不存在单独的恢复路径（见 [Fold 与快照](fold-and-snapshot.md)）。

## 投递保证

投递是**持久的恰好一次**，由至少一次投递加上幂等消费拼装而成。匹配到的唤醒由 Dispatcher 持久持有，其寿命超越任何单个租约：租用不会消费它。只有一次消费型释放才清除它——`release(consumed_wake_event=…)`——它发生在 `TaskWoken` 信封安全写入日志之后。如果 Worker 在租用之后、那次写入之前崩溃，过期租约清扫会把 Task 连同其完好的唤醒送回就绪队列，下一次租约会再次投递同一个唤醒。重复投递是幂等的：Worker 会在当前挂起窗口内寻找一个与这个唤醒匹配的 `TaskWoken`，如果已经落地了一个，它就与 fold 出的状态对账，而不是写第二个。

定时器唤醒没有外部生产者：Worker 按间隔调用 `Dispatcher.fire_due_timers(now=…)`，与过期清扫同步进行，Dispatcher 把每个到期的定时器挂起翻回就绪。

一个没有排队唤醒的挂起 Task 不是错误——它只是在等待尚未发生的事情。Worker 会把它以 `suspended` 重新释放，`wake_on` 得到保留，并发出一个 `suspended_without_wake` 可靠性信号：这是进程本地的可观测性，不是一个 EventLog 事件，也不是一条丢失路径。

## 这个保证能扩展到多远

这个保证对**多个并发 Worker** 成立，而不只是单个。匹配和消费之间任意时刻的崩溃都会解析为恰好一个持久的 `TaskWoken`，竞争的 Worker 也不会双写：每一次带 lease 校验的 append 都被 fencing 保护，因此一个租约已被回收的滞留 Worker 会被拒绝，而不是被允许把写落在更晚一代租约之后。

两种部署范围：

- **单主机、多 Worker** —— 每个后端。一个 host 运行一个常驻的 `WorkerLoop` 池。
- **多主机** —— Postgres，其中 fence 是在插入事件的同一事务内，对 dispatcher 行做的一次事务内 `SELECT … FOR SHARE`，且过期时间与数据库时钟比较，因此各主机的时钟偏差不会造成脑裂。SQLite 和内存后端按定义是单主机的；在那一台主机上开一个 Worker 池没问题，但把两个主机进程指向同一个 SQLite 文件是不支持的。

一次**步骤中途**的崩溃——在 `TaskWoken` 之后、该 Step 其余事件落地之前——会在下一次租约时恢复。被中断的尝试会被一个持久的 `StepAttemptAbandoned` 标记封存，该标记携带尝试前的基线，随后被分类：一个没有记录任何有副作用活动的尝试会被自动重新驱动；其他任何情况都会把 Task 停放为一个已停止的对话，附带一条 `origin="system"` 的通知，停在下一目标唤醒句柄上，因此打字就能恢复它。一个窗口内连续三次封存会无条件强制停放，因此一个崩溃循环不会永远重试下去。

恢复范围、SQLite 的单主机边界，以及仅剩的那个开放边缘——sandbox 副作用在 Worker 代际之间不受 fencing 保护——都编录在[已知限制](../operations/limitations.md)里；fencing 的论证见 [multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)，封存并分类的规则见 [step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)。

相关：[任务模型](task-model.md) ·
[引擎与执行](engine-execution.md) ·
[Fold 与快照](fold-and-snapshot.md)
