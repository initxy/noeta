# 任务与唤醒

**Task** 就是 agent 的一次运行，也是 Noeta 唯一的工作单元。持续几周的对话、每晚的定时作业、委派出去的子代理，都是 Task。Task 需要等待时——等人、等定时器、等子任务、等外部事件——它会**挂起**：等待期间什么都不占，等的东西到了，只被唤醒一次。

<NtTaskStates lang="zh" />

## 保证什么

- **所有等待都是同一个状态。** 不管等什么，都是 `suspended` 加一个带类型的唤醒条件，恢复逻辑只有一套。
- **等待没有开销。** 挂起的 Task 不占线程、连接和进程内存，等几秒或几个月都行。
- **唤醒持久保存，只生效一次。** 匹配上的唤醒扛得住 worker 崩溃，并且只会被消费一次。
- **只有一个写入方。** worker 运行 Task 时持有租约；租约被收回的 worker 写不进去。

## 四种状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 刚创建或刚被唤醒，等 worker 来取 |
| `running` | 某个 worker 持有租约，Engine 正在推进 |
| `suspended` | 在等 `wake_on` 里记录的唤醒条件 |
| `terminal` | 已结束，最后一条是 `TaskCompleted`、`TaskFailed` 或 `TaskCancelled` |

多轮对话就是一个 Task：每一轮是「唤醒 → 走几步 → 挂起」，两轮之间 Task 停在 `suspended`，等下一条用户消息；只有 `query()`（以及设了 `multi_turn=False` 的 `Client`）会以 `TaskCompleted` 结束。还没结束的 Task 在任何状态下都能取消。没有单独的 session 或 workflow 对象；宿主如果要给用户展示「会话」，自己在上层做。

## Task 能等什么

Task 存下的条件和最终满足它的事件是同一个 dataclass。是否匹配只看标识字段，其余内容只是顺带传过去。

| 条件 | 由谁送达 | 匹配依据 |
| --- | --- | --- |
| `SubtaskCompleted` | `ChildLifecycleObserver` | `subtask_id` |
| `SubtaskGroupCompleted` | `ChildLifecycleObserver` | `group_id` |
| `HumanResponseReceived` | 你面向用户的渠道 | `handle` |
| `TimerFired` | worker 的定时器轮询 | `event.fire_at >= condition.fire_at` |
| `ExternalEvent` | 任意外部来源 | `event_kind` |

所有 dispatcher 都调用同一个 `matches_wake`，不同存储后端对「是否匹配」不会有分歧。

## 唤醒怎么送达

<NtWake lang="zh" />

1. 唤醒事件经 `Dispatcher.wake` 进来（定时器走 `Dispatcher.fire_due_timers`）。匹配上的话，dispatcher 把匹配结果存下来，把 Task 放回就绪队列。
2. 下一个拿到租约的 worker 从 `Lease.wake_event` 收到这个唤醒。
3. Engine 写入 `TaskWoken` 事件，这一步写成功才算数。
4. 之后 worker 才释放租约，同时标记这个唤醒已消费。

如果 worker 在第 2 步到第 4 步之间死掉，存下的匹配结果还在；过期检查把 Task 放回队列，下一次拿租约时送达的还是同一个唤醒。如果 `TaskWoken` 已经写进去了，worker 会看到它，不再写第二条。「至少送达一次」加上「重复处理无副作用」，结果就是只生效一次。

定时器不需要外部触发：每个 worker 定期调用 `Dispatcher.fire_due_timers(now=…)`，和过期检查一起跑。一个挂起的 Task 暂时没有唤醒不算错误，它只是还在等。

## 能扩到多大

| 部署方式 | 支持情况 |
| --- | --- |
| 一台机器，一组 worker | 所有后端（内存、SQLite、Postgres） |
| 多台机器共用一个存储 | 只有 Postgres；租约检查和写事件在同一个事务里做，用的是数据库时钟 |

不支持让两个宿主进程共用一个 SQLite 文件。

## 子任务

子任务就是普通的 Task，有自己的日志，和父任务的关系只靠 `parent_task_id`；`subtask_depth` 受预算限制，委派不会无限递归。父任务挂起等 `SubtaskCompleted`（并行分发时等 `SubtaskGroupCompleted`），子任务的结果就是唤醒内容。每个节点各自恢复。整棵树的根 `root_task_id` 负责那些比单步活得久的东西：后台 shell、后台子代理、沙箱容器。

## 对你意味着什么

- 问用户、等定时器、委派子任务都不用一直占着资源；答复可以几天后在另一台机器上到达。
- 要跑一组 `WorkerLoop`，才有人拿租约、做过期检查、触发定时器，见[部署](../guides/deploy.md)和 [worker loop 参考](../reference/worker-loop.md)。
- 只要有不止一个宿主进程分担工作，就用 Postgres。

设计记录：
[task as the only primitive](https://github.com/initxy/noeta/blob/main/docs/adr/task-as-only-primitive.md) ·
[durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md) ·
[multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)

## 下一步

- [Engine](engine.md)：Task 在 `running` 时做什么。
- [委派子代理](../guides/subagents.md)：父子任务的实际用法。
- [事件日志](event-log.md)：被唤醒的 Task 怎么拿回状态。
