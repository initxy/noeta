# WorkerLoop 参考

常驻排空循环，作为库原语 `noeta.runtime.worker.WorkerLoop` 交付。没有控制台脚本，也没有东西为你启动它——嵌入 host 构造并运行它，并通过对同一个存储跑多个循环（各带自己的 `worker_id`）来扩容（参见[部署 worker](../how-to/deploy-worker.md)）。

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
loop.run_forever(install_signals=True)   # blocks until stop()
```

成员按名字引用，而非行号——行号每次编辑都会漂移，所以模块路径加成员名才是稳定坐标。

## `WorkerRuntime` 协议

循环驱动任何暴露四个只读属性的对象：`engine`、`event_log`、`content_store`、`dispatcher`。仓库内的 `noeta.testing.profile.RuntimeBundle` 满足它。另有三个方法是**鸭子类型（duck-typed）**的——省略它们的运行时会退化为无操作：

| 方法 | 存在时的效果 |
| --- | --- |
| `resolve_engine(task) → Engine` | 多代理主机提供的每任务引擎解析器；没有它循环始终使用单个 `rt.engine`，因此一个循环绑定一个 provider / 模型 / 工具集 / 策略 |
| `settle_subtasks_after_step(task_id)` | 推进刚驱动的任务所拦挡（barrier）的一棵委派子树（常驻路径没有请求内的排空来为它的子任务播种） |
| `take_pending_prelude(task_id)` | 交出一个 host 在 seed-yield 时刻暂存的、一次性的非持久唤醒 prelude |

存储中的任务必须与排空它的循环兼容（就绪队列没有路由）：给不同的配置文件各自的 sqlite 文件。

为运行时的存储使用**真实的 sqlite 文件**——跨进程入队只有通过共享的磁盘上状态才能工作；`:memory:` 仅用于开发 / 测试。

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
| `worker_id` | 租约所有者 id |
| `lease_seconds` | 每个任务授予的初始租约截止时间 |
| `poll_interval` | 就绪队列为空时的睡眠时间 |
| `heartbeat_interval` | 每步租约保活节奏（`<= 0` 禁用） |
| `stale_sweep_interval` | `requeue_stale` 清扫节奏（`<= 0` 禁用） |
| `timer_poll_interval` | `fire_due_timers` 轮询节奏（`TimerFired` 生产者；`<= 0` 禁用） |
| `shutdown_grace_s` | `stop()` 后等待进行中步骤的最大时间，然后**放弃**；`None` / `<= 0` = 无限等待 |
| `sleep` / `clock` / `now_fn` / `heartbeat_wait` | 可注入的时间 seam（测试用）；`now_fn` 是定时器到期检查使用的**墙钟**，与单调 `clock` 分开 |
| `reliability_sink` | `ReliabilityEvent` 的去向；默认：结构化日志 |
| `step_poll_s` | 等待进行中步骤线程时的轮询节奏 |
| `next_goal_handle` | 设置后，一次人工关闭 / 取消会把任务挂起在这个 handle 上（再次输入即可重开），而不是终态释放它 |

**没有 `workers` 旋钮**：一个 `WorkerLoop` 就是一条 drain 线程。要扩容就对同一个存储跑多个循环（各带自己的 `worker_id`）。并发循环是安全的：带 lease 校验的 append 受 fencing 保护，租约被回收的循环无法把写落在接手它的那个循环之后。

## 方法与属性

| 成员 | 行为 |
| --- | --- |
| `run_forever(*, install_signals=False)` | 驱动直到 `stop()`；每次迭代：`maybe_sweep()` → `maybe_poll_timers()` → `tick()`，空闲时睡眠 `poll_interval`。`install_signals=True` 将 SIGTERM/SIGINT 连接到 `stop()`（仅限主线程）并在退出时恢复处理器 |
| `tick() → bool` | 租约一个就绪任务并推进一步；队列为空时 `False`。异常策略在内部应用 |
| `maybe_sweep() → bool` | 如果间隔已过则运行 `requeue_stale()` |
| `maybe_poll_timers() → bool` | 如果间隔已过则运行 `fire_due_timers()`；在没有定时器的 dispatcher 上退化为无操作 |
| `stop()` | 发信号让循环在当前迭代后停止 |
| `running: bool` | 循环仍在运行 |
| `abandoned: bool` | 当关闭宽限期已过但步骤仍在进行中时设置。主机**必须退出进程**——被放弃的步骤线程可能仍在写入 EventLog；不支持进程内重用 |

模块级辅助函数：

- `install_stop_signals(loop) → restore()` —— 将 SIGTERM/SIGINT 连接到 `loop.stop()`；不在主线程上时它会警告并返回一个无操作的 restore。
- `run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None, reliability_sink=None, engine=None) → WorkerOutcome` —— 规范的 3 态恢复机（包括崩溃恢复的密封 / 重新驱动 / 停放），与进程内运行器共享，使两者无法漂移。
- `keep_lease_alive(dispatcher, lease, *, interval=30.0, lease_seconds=600.0, reliability_sink=None)` —— 每步心跳上下文管理器，供那些在没有常驻循环环绕的情况下驱动一个已租约步骤的调用方使用。
- `resolve_engine(rt, task) → Engine` —— 每任务解析器背后的 seam。

## 异常策略

常驻循环不能因中毒任务而崩溃：

- `InvalidLease` → 记录日志 + 继续；不 `release` / `fail`（租约不属于我们）。
- 任何其他异常 → `dispatcher.fail(lease_id, retryable=True, reason=…)`：有界重试，最多到后端的 `max_fail_attempts`，然后终止。
- 如果 `fail()` 本身抛出 → 记录日志 + 继续。
- 循环始终前进到下一个任务。

Provider 失败从不抵达这个兜底：`runtime/llm.py` 把 provider 异常翻译成一个策略能读取的错误 `LLMResponse`，所以重试在那里被消耗，而不是在这里重复计数。

## 结果与可靠性类型

`WorkerOutcome`：`"woken" | "drained" | "skipped" | "cancelled" | "stopped"`。`"skipped"` 意味着一个挂起的任务尚无唤醒（诊断信息，不是错误）；`"cancelled"` / `"stopped"` 意味着人工取消 / 关闭在轮次中途到达——`"cancelled"` 让任务终态，`"stopped"` 让任务可重开。`"stopped"` 还涵盖崩溃恢复的**停放**：任务带着系统通知保持挂起，输入一条消息即可恢复它。

`ReliabilityEvent` —— 进程本地信号（**不是** EventLog 事件），发送到 `reliability_sink`。种类：`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`、`attempt_parked`（后两个是崩溃恢复时刻：一个被中断的尝试被密封并自动重新驱动，或被密封并为人工处理而停放）。每一种都只命名循环能从 Dispatcher seam 实际证明的事，从不命名它无法观测的根因。

`WakeRecoveryError` —— 一个被唤醒的租约的唤醒无法与 fold 后的状态协调；worker 大声失败。步骤中途崩溃**不是**错误路径：在下一次租约时，被中断的尝试以 `StepAttemptAbandoned` 密封，且当它按审批面判定为无副作用时自动重新驱动，否则任务被停放等待人工处理（参见 [已知限制](../operations/limitations.md)）。

## 关闭语义

`stop()` 停止租约，并等待进行中步骤最多 `shutdown_grace_s`（其租约由心跳保活）。超时时循环**放弃**该步骤：停止其心跳、发出 `shutdown_abandoned`、设置 `abandoned`，然后返回，不释放也不使租约失败。Python 无法中断步骤线程——放弃之所以安全只是因为进程退出了；然后租约过期，`requeue_stale` 在下次启动时回收任务。

心跳不能永远延长租约：dispatcher 把延长次数封顶在 `heartbeat_max`，因此 `heartbeat_interval × heartbeat_max` 界定了一步的持有时间；超过上限后租约被强制释放，步骤的下一次写入以 `InvalidLease` 失败。边界条件——SQLite 的单主机限制、崩溃恢复范围——在 [已知限制](../operations/limitations.md) 中有编目。

## 另见

- [唤醒与恢复](../concepts/wake-resume.md) — 交付保证
- [架构概览](../architecture/overview.md) — 唤醒机制
- [操作指南：部署 worker](../how-to/deploy-worker.md)
