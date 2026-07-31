# WorkerLoop

worker 就是那个把一个等待中的任务取走、并把它往前推一步的东西。`WorkerLoop` 就是这个循环，作为库原语交付：它租用一个就绪任务、推进它、释放，然后重复——外加心跳、过期租约清扫、定时器轮询和一次有界的优雅停机。

没有控制台脚本，也没有任何东西替你启动它。嵌入方宿主自己构造并运行它，并通过对同一个存储跑多个循环——每个带自己的 `worker_id`——来扩容。

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
print(loop.running)                      # → False
loop.run_forever(install_signals=True)   # blocks until stop()
```

如果你只是想在一个已有的 `Client` 里要几个 worker，请改调 `client.start_workers(n)`——见 [query / Client](sdk-client.md)。

下面的成员按名字给出，而不是按行号：行号每次编辑都会漂移，因此模块路径加成员名才是稳定坐标。

## `WorkerRuntime` protocol

这个循环可以驱动任何暴露了四个只读属性的对象：`engine`、`event_log`、`content_store`、`dispatcher`。仓库内的 `noeta.testing.profile.RuntimeBundle` 满足它。另有三个方法是**鸭子类型**的——一个不提供它们的 runtime 会退化为空操作：

| 方法 | 存在时的效果 |
| --- | --- |
| `resolve_engine(task) → Engine` | 多 agent 宿主提供的按任务 engine 解析器；没有它，循环总是使用那唯一的 `rt.engine`，因此一个循环绑定一套 provider / 模型 / 工具集 / policy |
| `settle_subtasks_after_step(task_id)` | 驱动刚被推进的任务所栅栏等待的那棵委派子树（常驻路径没有请求内的排空来 seed 它的子任务） |
| `take_pending_prelude(task_id)` | 交出宿主在 seed-yield 时暂存的一次性、非持久的唤醒前导 |

一个存储里的任务必须与排空它的那个循环兼容（就绪队列没有路由）：不同的 profile 请各用各的 sqlite 文件。

runtime 的存储请用一个**真实的 sqlite 文件**——跨进程入队只能通过共享的磁盘状态实现；`:memory:` 仅限开发/测试。

## 构造函数

```python
WorkerLoop(
    rt: WorkerRuntime,
    *,
    worker_id: str = "noeta-worker",
    lease_seconds: float = 600.0,
    poll_interval: float = 0.5,
    heartbeat_interval: float = 30.0,
    stale_sweep_interval: float = 10.0,
    timer_poll_interval: float = 1.0,
    shutdown_grace_s: Optional[float] = DEFAULT_SHUTDOWN_GRACE_S,   # 30.0
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    now_fn: Optional[Callable[[], float]] = None,
    heartbeat_wait: Optional[Callable[[float], bool]] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
    step_poll_s: float = 0.05,
    next_goal_handle: Optional[str] = None,
)
```

| 旋钮 | 含义 |
| --- | --- |
| `worker_id` | 租约持有者 id |
| `lease_seconds` | 每个任务被授予的初始租约期限 |
| `poll_interval` | 就绪队列为空时的睡眠时长 |
| `heartbeat_interval` | 每步的租约保活节奏（`<= 0` 关闭） |
| `stale_sweep_interval` | `requeue_stale` 清扫的节奏（`<= 0` 关闭） |
| `timer_poll_interval` | `fire_due_timers` 轮询的节奏（`TimerFired` 的生产者；`<= 0` 关闭） |
| `shutdown_grace_s` | `stop()` 之后对进行中步骤的最长等待，超时则**放弃**；`None` / `<= 0` = 无限等待 |
| `sleep` / `clock` / `now_fn` / `heartbeat_wait` | 可注入的时间接缝（测试用）；`now_fn` 是定时器到期检查所用的**墙上**时钟，与单调的 `clock` 分开 |
| `reliability_sink` | `ReliabilityEvent` 的去向；默认：结构化日志 |
| `step_poll_s` | 等待进行中步骤线程时的轮询节奏 |
| `next_goal_handle` | 设置后，一次人类的 close / cancel 会把任务挂在这个句柄上（再打字即可重开），而不是把它以终止状态释放 |

**没有 `workers` 这个旋钮**：一个 `WorkerLoop` 就是一条排空线程。你通过对同一个存储跑多个循环（每个带自己的 `worker_id`）来扩容。并发的循环是安全的：带 lease 校验的 append 有 fencing 保护，因此一个租约已被回收的循环无法把写落在接手它的那个循环之后。

## 方法与属性

| 成员 | 行为 |
| --- | --- |
| `run_forever(*, install_signals=False)` | 一直驱动到 `stop()`；每次迭代：`maybe_sweep()` → `maybe_poll_timers()` → `tick()`，空闲时睡 `poll_interval`。`install_signals=True` 把 SIGTERM/SIGINT 接到 `stop()`（仅限主线程），并在退出时恢复处理器 |
| `tick() → bool` | 租用一个就绪任务并把它推进一步；队列为空时返回 `False`。异常策略在内部生效 |
| `maybe_sweep() → bool` | 若间隔已到则运行 `requeue_stale()` |
| `maybe_poll_timers() → bool` | 若间隔已到则运行 `fire_due_timers()`；在没有定时器的 dispatcher 上退化为空操作 |
| `stop()` | 通知循环在当前这次迭代之后停下 |
| `running: bool` | 循环是否仍在运行 |
| `abandoned: bool` | 当停机宽限期耗尽而仍有步骤在飞行中时被置位。宿主**必须让进程退出**——那个被放弃的步骤线程可能还会写 EventLog；不支持在进程内继续复用 |

模块级辅助函数：

- `install_stop_signals(loop) → restore()` —— 把 SIGTERM/SIGINT 接到 `loop.stop()`；不在主线程时它会告警并返回一个空操作的 restore。
- `run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None, reliability_sink=None, engine=None) → WorkerOutcome` —— 规范的三态恢复机（含崩溃恢复的封存 / 重新驱动 / 停放），与进程内 runner 共享，因此两者不会漂移。
- `keep_lease_alive(dispatcher, lease, *, interval=30.0, lease_seconds=600.0, reliability_sink=None)` —— 每步的心跳上下文管理器，供那些在没有常驻循环包裹的情况下驱动一个租约步骤的调用方使用。
- `resolve_engine(rt, task) → Engine` —— 按任务解析器背后的那个接缝。

## 异常策略

一个常驻循环不能因为一个"有毒"的任务而崩溃：

- `InvalidLease` → 记日志并继续；不做 `release` / `fail`（那个租约不是我们的）。
- 其他任何异常 → `dispatcher.fail(lease_id, retryable=True, reason=…)`：有界重试直到后端的 `max_fail_attempts`，然后转终止。
- 如果 `fail()` 自身抛出 → 记日志并继续。
- 循环总是继续处理下一个任务。

Provider 故障永远不会走到这个兜底：`runtime/llm.py` 把一个 provider 异常翻译成一个 policy 会读到的错误 `LLMResponse`，因此重试在那里被消化掉，而不是在这里被重复计数。

## Outcome 与可靠性类型

`WorkerOutcome`：`"woken" | "drained" | "skipped" | "cancelled" | "stopped"`。`"skipped"` 表示一个尚无唤醒的挂起任务（一个诊断信息，不是错误）；`"cancelled"` / `"stopped"` 表示一次人类的 cancel/close 落在了轮次中途——`"cancelled"` 让任务停在终止状态，`"stopped"` 让它保持可重开。`"stopped"` 也涵盖崩溃恢复中的**停放**：任务带着一条系统通知停在挂起状态，打一条消息就能恢复它。

`ReliabilityEvent` —— 进程本地的信号（**不是** EventLog 事件），发往 `reliability_sink`。种类有：`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`、`attempt_parked`（后两个是崩溃恢复的两个时刻：一次被中断的尝试被封存并自动重新驱动，或者被封存并停放等人处理）。每一种都只指称这个循环真正能从 Dispatcher 接缝上证明的事，而绝不指称它观察不到的根因。

`WakeRecoveryError` —— 一个已唤醒租约上的唤醒无法与 fold 出的状态对账；worker 会大声失败。步骤中途的一次崩溃**不是**错误路径：在下一次租约时，被中断的尝试会被 `StepAttemptAbandoned` 封存，并在按审批面判定它无副作用时自动重新驱动，否则任务被停放等人处理（见[已知限制](../operations/limitations.md)）。

## 停机语义

`stop()` 停止租用，并为进行中的步骤最多等待 `shutdown_grace_s`（其租约由心跳保活）。超时后循环**放弃**这个步骤：停掉它的心跳、发出 `shutdown_abandoned`、置位 `abandoned`，然后在既不释放也不失败这个租约的情况下返回。Python 无法中断那个步骤线程——放弃之所以安全，只是因为进程会退出；随后租约过期，`requeue_stale` 会在下次启动时把这个任务回收。

心跳不能无限延长一个租约：dispatcher 把延长次数限制在 `heartbeat_max`，因此 `heartbeat_interval × heartbeat_max` 限住了一个步骤的持有时长；超过上限后租约被强制释放，该步骤的下一次写入会以 `InvalidLease` 失败。边界条件——SQLite 的单主机限制、崩溃恢复的范围——都编录在[已知限制](../operations/limitations.md)里。

## 下一步

- [部署 Worker](../how-to/deploy-worker.md) —— 面向任务的指南
- [唤醒与恢复](../concepts/wake-resume.md) —— 持久、单 worker、恰好一次的投递保证
- [query / Client](sdk-client.md) —— 进程内池的那个替代方案
- [已知限制](../operations/limitations.md) —— 各项边界条件
