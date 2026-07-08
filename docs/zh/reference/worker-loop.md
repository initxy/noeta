# WorkerLoop 参考

常驻排空循环，作为库原语 `noeta.runtime.worker.WorkerLoop` 交付（`packages/noeta-runtime/noeta/runtime/worker.py:752`）。没有控制台脚本，也没有东西为你启动它——嵌入者构造并运行它。注意 `python -m noeta.agent` **不**启动 `WorkerLoop`；聊天服务器内联驱动轮次（参见 [编码代理手册](noeta-agent.md)）。

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
loop.run_forever(install_signals=True)   # 阻塞直到 stop()
```

## `WorkerRuntime` 协议 — `worker.py:205`

循环驱动任何暴露四个只读属性的对象：`engine`、`event_log`、`content_store`、`dispatcher`。仓库内的 `noeta.testing.profile.RuntimeBundle` 满足它。多代理主机可以额外提供 `resolve_engine(task) → Engine` —— 每任务解析器 seam（`worker.py:237`）；没有它循环始终使用单个 `rt.engine`，因此一个循环绑定一个 provider / 模型 / 工具集 / 策略。存储中的任务必须与排空它的循环兼容（就绪队列没有路由）：给不同的配置文件各自的 sqlite 文件。

为运行时的存储使用**真实的 sqlite 文件**——跨进程入队只有通过共享的磁盘上状态才能工作；`:memory:` 仅用于开发 / 测试。

## 构造函数 — `worker.py:775-792`

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
    shutdown_grace_s: Optional[float] = 30.0,   # DEFAULT_SHUTDOWN_GRACE_S (worker.py:79)
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    now_fn: Optional[Callable[[], float]] = None,
    heartbeat_wait: Optional[Callable[[float], bool]] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
    step_poll_s: float = 0.05,
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

**没有 `workers` 旋钮**——循环按设计是单 worker 的。

## 方法与属性

| 成员 | 行为 | 源码 |
| --- | --- | --- |
| `run_forever(*, install_signals=False)` | 驱动直到 `stop()`；每次迭代：`maybe_sweep()` → `maybe_poll_timers()` → `tick()`，空闲时睡眠 `poll_interval`。`install_signals=True` 将 SIGTERM/SIGINT 连接到 `stop()`（仅限主线程）并在退出时恢复处理器 | `worker.py:1093` |
| `tick() → bool` | 租约一个就绪任务并推进一步；队列为空时 `False`。异常策略在内部应用 | `worker.py:864` |
| `maybe_sweep() → bool` | 如果间隔已过则运行 `requeue_stale()` | `worker.py:882` |
| `maybe_poll_timers() → bool` | 如果间隔已过则运行 `fire_due_timers()`；在没有定时器的 dispatcher 上退化为无操作 | `worker.py:906` |
| `stop()` | 发信号让循环在当前迭代后停止 | `worker.py:841` |
| `running: bool` | 循环仍在运行 | `worker.py:846` |
| `abandoned: bool` | 当关闭宽限期已过但步骤仍在进行中时设置。主机**必须退出进程**——被放弃的步骤线程可能仍在写入 EventLog；不支持进程内重用 | `worker.py:850` |

辅助函数：`install_stop_signals(loop) → restore()`；`run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None, reliability_sink=None, engine=None) → WorkerOutcome` —— 与进程内运行器共享的规范 3 态恢复机（包括崩溃恢复密封 / 重新驱动 / 停放）（`worker.py:421`）；`keep_lease_alive(...)` —— 每步心跳上下文管理器。

## 异常策略 — `worker.py:755-767`

常驻循环不能因中毒任务而崩溃：

- `InvalidLease` → 记录日志 + 继续；不 `release` / `fail`（租约不再属于我们）。
- 任何其他异常 → `dispatcher.fail(lease_id, retryable=True, reason=…)`：有界重试，然后终止。
- 如果 `fail()` 本身抛出 → 记录日志 + 继续。
- 循环始终前进到下一个任务。

## 结果与可靠性类型

`WorkerOutcome`（`worker.py:162`）：`"woken" | "drained" | "skipped" | "cancelled" | "stopped"` —— `"skipped"` 意味着一个挂起的任务尚无唤醒（诊断信息，不是错误）；`"cancelled"` / `"stopped"` 意味着人工取消 / 关闭在轮次中途到达。`"stopped"` 还涵盖崩溃恢复**停放**：任务带着系统通知保持挂起，输入消息可恢复它。

`ReliabilityEvent`（`worker.py:134`）—— 进程本地信号（**不是** EventLog 事件），发送到 `reliability_sink`。种类（`worker.py:122`）：`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`、`attempt_parked`（后两个是崩溃恢复时刻：被中断的尝试被密封并自动重新驱动，或被密封并停放等待人工处理）。

异常：`WakeRecoveryError`（`worker.py:167`）—— 被唤醒的租约的唤醒无法与 fold 后的状态协调；worker 大声失败。步骤中途崩溃**不是**错误路径：在下一次租约时，被中断的尝试以 `StepAttemptAbandoned` 密封，且当它按审批面判定为无副作用时自动重新驱动，否则任务被停放等待人工处理（参见 [已知限制](../operations/limitations.md)）。

## 关闭语义

`stop()` 停止租约，并等待进行中步骤最多 `shutdown_grace_s`（其租约由心跳保活）。超时时循环**放弃**该步骤：停止其心跳、发出 `shutdown_abandoned`、设置 `abandoned`，然后返回。Python 无法中断步骤线程——放弃之所以安全只是因为进程退出了；然后租约过期，`requeue_stale` 在下次启动时回收任务。

心跳不能永远延长租约：dispatcher 限制了延长次数，因此 `heartbeat_interval × heartbeat_max` 界定了一步的持有时间；超过限制后租约被强制释放，步骤的下一次写入以 `InvalidLease` 失败。边界条件——单 worker、崩溃恢复范围——在 [已知限制](../operations/limitations.md) 中有编目。

## 另见

- [唤醒与恢复](../concepts/wake-resume.md) — 交付保证
- [架构概览](../architecture/overview.md) — 唤醒机制
- [操作指南：部署 worker](../how-to/deploy-worker.md)
