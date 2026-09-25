# WorkerLoop

`WorkerLoop`（从 `noeta.sdk` 导入）循环做一件事：租一个就绪任务、推进一步、释放，再来——外加心跳续租、过期租约回收、定时器轮询和有上限的优雅退出。

没有东西会自动启动它。宿主自己构造并运行；要扩容就对同一个存储多跑几个循环，每个用不同的 `worker_id`。

```python
from noeta.sdk import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
print(loop.running)                      # → False
loop.run_forever(install_signals=True)   # blocks until stop()
```

在 `Client` 里直接用 `client.start_workers(n)` 就行——见 [SDK](sdk.md)。

## `WorkerRuntime`

循环能驱动任何带四个只读属性的对象：`engine`、`event_log`、`content_store`、`dispatcher`（`noeta.testing.profile.RuntimeBundle` 就是一个）。另有三个可选方法，有就用：

| 方法 | 有它时的作用 |
| --- | --- |
| `resolve_engine(task) -> Engine` | 按任务选引擎；没有则所有任务都用 `rt.engine` |
| `settle_subtasks_after_step(task_id)` | 推进刚走完一步的任务正在等的子任务 |
| `take_pending_prelude(task_id)` | 取走宿主暂存的一次性唤醒前置步骤 |

每个循环只从自己的 `queue` 里取任务，所以配置不同的几组 worker 可以共用一个存储；同一个队列里的任务必须都是这组 worker 能跑的（[ADR](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md)）。跨进程要用真实的 SQLite 文件或 Postgres，`:memory:` 只适合测试。

## 构造参数

| 参数 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `rt` | `WorkerRuntime` | 必填 | 要驱动的运行时 |
| `worker_id` | `str` | `"noeta-worker"` | 租约持有者 id |
| `lease_seconds` | `float` | `600.0` | 每个任务初始租期 |
| `poll_interval` | `float` | `0.5` | 队列空时的休眠间隔 |
| `heartbeat_interval` | `float` | `30.0` | 续租间隔；`<= 0` 关闭 |
| `stale_sweep_interval` | `float` | `10.0` | `requeue_stale` 间隔；`<= 0` 关闭 |
| `timer_poll_interval` | `float` | `1.0` | `fire_due_timers` 间隔；`<= 0` 关闭 |
| `shutdown_grace_s` | `float \| None` | `30.0` | `stop()` 后最多等正在跑的一步多久，超时就放弃；`None` / `<= 0` 一直等 |
| `sleep`、`clock`、`now_fn`、`heartbeat_wait` | 函数 | `None` | 测试用的时间注入点；`now_fn` 是定时器用的墙钟，`clock` 是单调时钟 |
| `reliability_sink` | `ReliabilitySink \| None` | `None` | 接收 `ReliabilityEvent`；默认写结构化日志 |
| `step_poll_s` | `float` | `0.05` | 等待执行线程时的轮询间隔 |
| `next_goal_handle` | `str \| None` | `None` | 设置后，人工停止会让任务挂在这个句柄上（还能继续），而不是直接结束 |
| `queue` | `str` | `"default"` | 只从这个队列取任务；要和客户端的 `HostConfig.queue` 一致 |
| `lease_backoff_max_s` | `float` | `30.0` | dispatcher 出错后翻倍退避的上限 |

一个循环就是一个执行线程，没有 `workers` 参数。多个循环并发是安全的：写入受租约保护。

## 方法

| 成员 | 行为 |
| --- | --- |
| `run_forever(*, install_signals=False)` | 先跑一次 `recover_cap_terminal()`，然后循环 `maybe_sweep()` → `maybe_poll_timers()` → `tick()`，直到 `stop()`。`install_signals=True` 接管 SIGTERM/SIGINT（仅主线程）。 |
| `tick() -> bool` | 租一个任务推进一步；队列空或 `lease()` 出错时返回 `False` |
| `maybe_sweep() -> bool` | 到点就跑 `requeue_stale()` |
| `maybe_poll_timers() -> bool` | 到点就跑 `fire_due_timers()`；不支持定时器时什么都不做 |
| `recover_cap_terminal() -> list[str]` | 启动时的补救检查；返回修好的任务 id |
| `stop()` | 当前这一轮跑完就停 |
| `running: bool` | 是否还在跑 |
| `abandoned: bool` | 退出宽限期到了还有一步没跑完——**必须退出进程** |

模块级函数：

| 函数 | 用途 |
| --- | --- |
| `install_stop_signals(loop) -> restore` | 把 SIGTERM/SIGINT 接到 `loop.stop()`；不在主线程时警告并返回空操作 |
| `run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None, reliability_sink=None, engine=None) -> WorkerOutcome` | 把一个已租到的任务推进一步，含崩溃恢复；进程内执行器也用它 |
| `keep_lease_alive(dispatcher, lease, *, interval=30.0, lease_seconds=600.0, reliability_sink=None)` | 心跳上下文管理器，给没有循环包着的单步执行用 |
| `resolve_engine(rt, task) -> Engine` | 按任务查引擎 |
| `reconcile_cap_terminal(rt, task_id) -> bool` | 给一个被上限卡掉的任务补写终态事件（重复调用无副作用） |
| `recover_cap_terminal(rt) -> list[str]` | 对所有任务做同样的事 |

## 出错时

| 情况 | 循环怎么做 |
| --- | --- |
| `InvalidLease` | 记日志继续；租约已经不是自己的 |
| 执行一步时抛其他异常 | `dispatcher.fail(lease_id, retryable=True, reason=…)`；重试到后端的 `max_fail_attempts` 为止，然后终止 |
| `fail()` 自己也抛异常 | 记日志继续 |
| `lease()` 时 dispatcher 出错 | 记日志、发 `dispatcher_unavailable`、翻倍退避（上限 `lease_backoff_max_s`），继续轮询 |
| `KeyboardInterrupt` / `SystemExit` | 照常抛出 |

模型调用出错到不了这里：它们会变成带错误的 `LLMResponse`，由决策循环处理。

**补写终态。** dispatcher 的上限（`max_fail_attempts` 或 `reclaim_max`）把任务行标成终止时，不会往事件日志里写东西，等它的父任务就永远醒不来。循环会在 `fail()` 之后、每次回收之后、以及启动时，给这类任务补写一条 `TaskFailed`，并发出 `cap_terminal_reconciled`。

## 结果和信号

`WorkerOutcome`：

| 取值 | 含义 |
| --- | --- |
| `"woken"` | 租约带着唤醒事件，任务推进了一步 |
| `"drained"` | 待执行或执行中的任务推进了一步 |
| `"skipped"` | 任务挂起着但还没被唤醒（只是诊断信息） |
| `"cancelled"` | 人工取消生效，任务已终止 |
| `"stopped"` | 人工停止生效，或崩溃恢复把它挂起；任务还能继续 |

`ReliabilityEvent(kind, task_id=None, lease_id=None, detail={})`——只在进程内，不写事件日志。取值：`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`（中断的一步已封存并自动重跑）、`attempt_parked`（已封存并挂起等人处理）、`cap_terminal_reconciled`、`dispatcher_unavailable`。

`WakeRecoveryError`——唤醒对不上折叠后的状态，worker 直接报错。执行中途崩溃不算错误：下次租到时会用 `StepAttemptAbandoned` 封存那一步，没有副作用就自动重跑，否则挂起等人处理。

## 退出

`stop()` 后不再租新任务，最多等 `shutdown_grace_s` 让正在跑的一步结束。超时就停掉心跳、发 `shutdown_abandoned`、把 `abandoned` 置真并返回，不释放租约；此时必须退出进程，下次启动时 `requeue_stale` 会把任务收回来。

心跳不能无限续租：dispatcher 限制最多续 `heartbeat_max` 次，超过后这一步的下一次写入会报 `InvalidLease`。

## 下一步

- [部署](../guides/deploy.md)——worker、Docker、Postgres
- [任务与唤醒](../how-it-works/tasks-and-waking.md)——挂起和唤醒怎么工作
- [已知限制](../operations/limitations.md)——边界情况
