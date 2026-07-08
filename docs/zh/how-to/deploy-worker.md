# 部署 worker

**目标：** 将 `WorkerLoop` 作为常驻排空循环运行，持续处理来自持久化存储的任务。

**开始之前：** 你已通过[你的第一个代理](../tutorials/first-agent.md)理解了 SDK。你已设置好持久化 SQLite 存储。

## WorkerLoop 是什么

`WorkerLoop` 是用于运行常驻代理的库原语，它排空调度器的就绪队列。它不是控制台脚本，也不是守护进程——你在自己的进程中构造并运行它：

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="my-worker")
loop.run_forever(install_signals=True)  # 阻塞直到 stop() 被调用
```

与 `python -m noeta.agent`（在 HTTP 处理器中内联驱动轮次）不同，`WorkerLoop` 是一个专用排空器：它轮询就绪队列，租约一个任务，推进一步，然后重复。这是需要比任何单个 HTTP 请求存活更久的代理的部署形态。

## 构建 WorkerRuntime

`WorkerLoop` 需要一个 `WorkerRuntime`——一个具有四个只读属性的对象：

```python
from dataclasses import dataclass

@dataclass
class MyRuntime:
    engine: ...        # noeta Engine 实例
    event_log: ...     # EventLog（为持久性使用 SQLite 后端）
    content_store: ... # ContentStore
    dispatcher: ...    # Dispatcher
```

仓库内的 `noeta.testing.profile.RuntimeBundle` 满足此协议，可用于测试。对于真实部署，请从运行时的存储和引擎模块组装这些组件。

### 使用真实 SQLite 存储

跨进程入队仅通过共享的磁盘状态工作。不要为常驻 worker 使用 `:memory:`：

```python
from noeta.storage.sqlite import (
    SqliteEventLog, SqliteContentStore, SqliteDispatcher,
)

db_path = "./worker.sqlite"
event_log = SqliteEventLog(db_path)
content_store = SqliteContentStore(db_path)
dispatcher = SqliteDispatcher(db_path)
```

三者必须指向同一个 SQLite 文件（或至少同一个磁盘数据库），这样调度器才能与 EventLog 协调。

## 构造并运行

```python
from noeta.runtime.worker import WorkerLoop, install_stop_signals

loop = WorkerLoop(
    rt,
    worker_id="noeta-worker",
    lease_seconds=600.0,
    poll_interval=0.5,
    heartbeat_interval=30.0,
    stale_sweep_interval=10.0,
    timer_poll_interval=1.0,
    shutdown_grace_s=30.0,
)

# 将 SIGTERM/SIGINT 连接到 stop()（仅主线程）
install_stop_signals(loop)

# 阻塞直到 stop() 被调用或信号到达
loop.run_forever(install_signals=True)
```

### 构造函数参数

| 参数 | 默认值 | 功能 |
| --- | --- | --- |
| `worker_id` | `"noeta-worker"` | 租约所有者标识符 |
| `lease_seconds` | `600.0` | 每个任务的初始租约截止时间 |
| `poll_interval` | `0.5` | 就绪队列为空时的休眠时间 |
| `heartbeat_interval` | `30.0` | 每步租约保活（`<= 0` 禁用） |
| `stale_sweep_interval` | `10.0` | `requeue_stale()` 执行频率（`<= 0` 禁用） |
| `timer_poll_interval` | `1.0` | `fire_due_timers()` 轮询频率 |
| `shutdown_grace_s` | `30.0` | `stop()` 后等待进行中步骤完成的最大时间。`None` = 无限制 |
| `reliability_sink` | 结构化日志 | `ReliabilityEvent` 的去向 |

**没有 `workers` 参数** — 该循环按设计是单 worker 的。

## 运行时发生了什么

`run_forever` 的每次迭代：

1. `maybe_sweep()` — 如果 `stale_sweep_interval` 已过，调用 `dispatcher.requeue_stale()` 回收租约已过期的任务。
2. `maybe_poll_timers()` — 如果 `timer_poll_interval` 已过，调用 `dispatcher.fire_due_timers()` 产生 `TimerFired` 唤醒事件。
3. `tick()` — 租约一个就绪任务并推进一步。如果队列为空则返回 `False`。
4. 如果 `tick()` 返回 `False`，休眠 `poll_interval` 秒。

## 关闭

调用 `loop.stop()` 以发出优雅关闭信号。循环会：

1. 停止租约新任务。
2. 等待最多 `shutdown_grace_s` 秒让进行中的步骤完成（其租约由心跳保活）。
3. 如果步骤未及时完成，**放弃**它：停止心跳，发出 `shutdown_abandoned`，设置 `loop.abandoned = True`。

当 `loop.abandoned` 为 `True` 时，主机**必须退出进程**。被放弃的步骤线程可能仍在写入 EventLog；放弃后不支持进程内重用。进程退出后，租约过期，`requeue_stale` 会在下次启动时回收该任务。

## 异常处理

常驻循环不能因中毒任务而崩溃：

- `InvalidLease` → 记录日志并继续（租约不再属于我们）。
- 任何其他异常 → `dispatcher.fail(lease_id, retryable=True, reason=…)`：有限重试，然后终止。
- 如果 `fail()` 本身抛出 → 记录日志并继续。

循环始终继续处理下一个任务。

## 另请参阅

- [WorkerLoop 参考](../reference/worker-loop.md) — 所有构造函数参数、方法和结果类型
- [唤醒与恢复](../concepts/wake-resume.md) — worker 实现的交付保证
- [已知限制](../operations/limitations.md) — 单 worker 边界和崩溃恢复范围
