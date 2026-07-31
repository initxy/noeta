# 唤醒与恢复

代理的大部分时间都花在等待上——等一个人回答、等一个子任务完成、等一个定时器、等系统之外的某件事。在 Noeta 里，一个正在等待的 Task 不持有线程、不持有连接，也不占用进程内存。它**挂起**：记下自己在等什么，然后彻底交出执行权。

之后，某个与那个条件匹配的东西到达，这个 Task 被重新入队，一个 Worker 取走它并继续。因为它的全部状态都从它的 EventLog fold 回来，恢复不需要任何专门的恢复代码——它就是每个租约步骤本来就要做的那次 fold（见 [Fold 与快照](fold-and-snapshot.md)）。

<p align="center">
  <img src="../../assets/diagrams/wake-resume.svg" alt="唤醒与恢复——任务在一个唤醒条件上挂起，一个匹配的唤醒事件到达，匹配被持久保留，任务恢复" width="820">
</p>

## 它在 Task 一生中的位置

挂起是四种状态之一，也是唯一表示"正在等待"的那一个——不管等的是什么：

<p align="center">
  <img src="../../assets/diagrams/task-lifecycle.svg" alt="任务生命周期——pending → running → suspended → terminal" width="820">
</p>

用一个状态、一条恢复路径来承载四种不同的等待，是一次刻意的简化。正因如此，比方说新增一种外部触发，并不会给生命周期新增一条分支。

## 一个 Task 可以等什么

四种等待对应五个条件 dataclass——子任务等待分单个子任务和分组两个变体。Task 存下的条件，和之后满足它的那个事件，是*同一个 dataclass*。存在 `wake_on` 上时它声明所等待的形态；经由 `Dispatcher.wake` 投递时它携带答案。

| 条件 | 由谁投递 | 匹配依据 |
| --- | --- | --- |
| `SubtaskCompleted` | `ChildLifecycleObserver` | `subtask_id`（子任务的 `result` 随行携带，供参考） |
| `SubtaskGroupCompleted` | `ChildLifecycleObserver` | `group_id`（成员的 `subtask_ids` 随行携带） |
| `HumanResponseReceived` | 面向人类的通道 | `handle` |
| `TimerFired` | Worker 的定时器轮询 | 阈值：`event.fire_at >= condition.fire_at` |
| `ExternalEvent` | 任意外部入口 | `event_kind` |

匹配是按**投影**进行的：只有右列里的标识字段参与。像 `SubtaskResult` 这样的负载字段随行携带只是为了提供信息，从不影响匹配是否成立。

`matches_wake` 是这张真值表的唯一实现，每个 Dispatcher 都经由它路由，因此没有任何适配器能私下产生分歧。跨类型的匹配永远为假——一个子任务唤醒无法满足一个定时器条件，无论它的字段说什么。

## 匹配之后会发生什么

一次匹配会把 Task 重新入队。下一个租用它的 Worker 在 `Lease.wake_event` 上收到这个唤醒，Engine 写入一个持久的 `TaskWoken` 信封，然后这一步运行。

## 投递保证

投递是**持久的恰好一次**，由至少一次投递加上幂等消费拼装而成。

匹配到的唤醒由 Dispatcher 持久持有，其寿命超越任何单个租约——租用不会消费它。只有一次*消费型*释放（`release(consumed_wake_event=…)`）才会清除它，而那发生在 `TaskWoken` 信封安全落入日志之后。

于是设想一个 Worker 在租用之后、那次写入之前崩溃。过期租约清扫会把这个 Task 连同完好的唤醒送回就绪队列，下一次租约会再次投递同一个唤醒。重复投递是幂等的：Worker 会在当前挂起窗口内寻找一个与这个唤醒匹配的 `TaskWoken`，如果已经落地了一个，它就与 fold 出的状态对账，而不是再写一个。无论哪条路，流上最终恰好有一个 `TaskWoken`，也不需要任何人手工介入。

两个配套细节：

- **定时器没有外部生产者。** Worker 按间隔调用 `Dispatcher.fire_due_timers(now=…)`，与过期清扫并行，Dispatcher 把每个到期的定时器挂起翻回就绪。
- **一个没有排队唤醒的挂起 Task 不是错误。** 它只是在等一件还没发生的事。Worker 会把它以 `suspended` 重新释放，`wake_on` 得到保留，并发出一个 `suspended_without_wake` 可靠性信号——这是进程本地的可观测性，不是一个 EventLog 事件。

## 这个保证能扩展到多远

它对**多个并发 Worker** 成立，而不只是一个。匹配与消费之间任意时刻的崩溃都会解析为恰好一个持久的 `TaskWoken`，而竞争的 Worker 也不会双写：每一次带 lease 校验的 append 都被 fencing 保护，因此一个租约已被回收的滞留 Worker 会被拒绝，而不是被允许把写落在更晚一代租约之后。

两种部署范围：

- **单主机、多 Worker** —— 每个后端都支持。宿主运行一个常驻的 `WorkerLoop` 池。
- **多主机** —— Postgres。fence 是在插入事件的同一事务内，对 dispatcher 行做的一次事务内 `SELECT … FOR SHARE`，且过期时间与数据库时钟比较，因此各主机的时钟偏差不会造成脑裂。SQLite 和内存后端按定义是单主机的：在那一台主机上开一个 Worker 池没问题，但把两个主机进程指向同一个 SQLite 文件是不支持的。

## 步骤中途崩溃

一次发生在 `TaskWoken` *之后*、这一步其余事件落地之前的崩溃，由更下一层处理。在下一次租约时，被中断的尝试会被一个携带尝试前基线的持久 `StepAttemptAbandoned` 标记封存，随后被分类：

- 一次没有记录任何有副作用活动的尝试会被自动重新驱动；
- 其他任何情况都会把 Task 停放为一个已停止的对话，附带一条 `origin="system"` 的通知，停在下一目标唤醒句柄上，因此再次打字就能恢复它。

一个窗口内连续三次封存会无条件强制停放，因此一个崩溃循环不会永远重试下去。

恢复范围、SQLite 的单主机边界，以及仅剩的那个开放边缘——sandbox 副作用在 Worker 代际之间不受 fencing 保护——都编录在[已知限制](../operations/limitations.md)里。fencing 的论证见 [multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)，封存并分类的规则见 [step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)。

## 下一步

- [任务模型](task-model.md) —— 本页在其间移动的那些状态。
- [Fold 与快照](fold-and-snapshot.md) —— 一个被恢复的 Task 如何取回自己的状态。
- [部署 Worker](../how-to/deploy-worker.md) —— 运行那个负责租用、清扫和触发定时器的循环。
- [Worker loop](../reference/worker-loop.md) —— `WorkerLoop` 上的各个旋钮。
