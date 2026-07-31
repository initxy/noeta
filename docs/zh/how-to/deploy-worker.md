# 部署 Worker

**目标：** 运行一个常驻的 Worker 池，持续排空一个持久化存储，让任务在创建它们的那个请求消失之后仍能推进。

**开始之前：** 你已通过[你的第一个 Agent](../tutorials/first-agent.md)理解了 SDK。

## 为什么你需要它

任务只有在有东西把它从 Dispatcher 的就绪队列里租走时才会推进。没有谁会替你启动那个排空过程。没有一个运行中的 Worker：

- `wait_timer` 挂起永远不会醒 —— Worker 的定时器轮询是 `TimerFired` 的唯一生产者；
- 一个 Worker 在中途崩溃的任务永远不会被回收，因为过期租约的清扫跑在排空循环内部；
- 一个进程置为就绪的任务永远不会被另一个进程接走。

Worker 就是任何"必须比创建它的请求活得更久"的东西的部署形态。

## 使用持久化存储

跨进程交接只有通过共享的磁盘状态才能工作，所以一个常驻池需要真实存储。`HostConfig.storage_path` 接受一个字符串 —— 一个 SQLite 文件路径、一个 `postgresql://` DSN，或 `":memory:"` —— 并按正确顺序构建整个 `(EventLog, ContentStore, Dispatcher)` 三元组：

```python
from pathlib import Path

from noeta.sdk import Client, HostConfig, Options

options = Options(system_prompt="You are a helpful assistant.", name="main")

client = Client(
    options,
    provider=my_provider,
    workspace_dir=Path("./workspace"),
    model="claude-sonnet-4-5-20250929",
    host_config=HostConfig(storage_path="./noeta.sqlite"),
)
```

不要给常驻池用 `":memory:"` —— 那个存储会随进程一起消亡，别的东西谁也看不见它。

如果你要自己构建这个三元组，用 `noeta.sdk.storage`，并让三个组件都待在同一个数据库上；EventLog 会把 Dispatcher 当作它的租约校验器：

```python
from noeta.sdk.storage import build_storage_stack

event_log, content_store, dispatcher = build_storage_stack(
    "sqlite", path="./noeta.sqlite",
)
```

`build_storage_stack` 接受 `"memory"`（无配置）、`"sqlite"`（`path=`）和 `"postgres"`（`dsn=`）。`open_storage_stack` 跑的是与 `HostConfig.storage_path` 相同的按值形状分派。把这个三元组作为 `HostConfig` 的 `event_log` / `content_store` / `dispatcher` 字段传入 —— 要么三个全给，要么一个都不给；把它们与 `storage_path` 混用会抛 `ValueError`。

## 启动与停止池

```python
with client:
    client.start_workers(4)
    ...
    client.stop_workers(timeout=30.0)
```

每个 Worker 跑在它自己的守护线程上，带着自己的 `worker_id`，而它们全都排空同一个就绪队列。并发 Worker 是安全的：每一次经租约校验的 append 都受 fencing 保护，所以一个租约被回收的 Worker 会被拒绝，而不是被允许写入。

`start_workers` 是一次性的 —— 第二次调用会抛 `RuntimeError`。`stop_workers` 向每个循环发信号并 join 各线程，当它们全部在 `timeout` 内退出时返回 `True`。超时时它返回 `False`，并特意继续跟踪这个池，好让一次重试能把活干完，而不是在第一个池上再叠一个池。`Client.shutdown`（因而也包括离开 `with` 块）会在拆除其他任何东西之前先停掉这个池。

### 参数

| 参数 | 默认值 | 功能 |
| --- | --- | --- |
| `num_workers` | `1` | 排空线程的数量。必须 `>= 1`。 |
| `poll_interval` | `0.1` | 就绪队列为空时的休眠时间 |
| `heartbeat_interval` | `30.0` | 每步的租约保活 |
| `stale_sweep_interval` | `10.0` | `requeue_stale()` 的执行节奏 |
| `timer_poll_interval` | `1.0` | `fire_due_timers()` 的执行节奏 |
| `lease_seconds` | `600.0` | 每个任务的初始租约截止时间 |
| `shutdown_grace_s` | `10.0` | 停止后等待进行中步骤的最大时间。`None` = 无限制 |

## Worker 每次迭代做什么

1. 清扫过期租约 —— `requeue_stale()` 回收租约已过期的任务。
2. 轮询定时器 —— `fire_due_timers()` 把每个到期的 `wait_timer` 挂起翻回就绪，并带上一个 `TimerFired` 唤醒。
3. 租约一个就绪任务，并把它推进一步。
4. 如果队列为空，休眠 `poll_interval`。

一个"中毒"的任务永远不会让循环崩溃。`InvalidLease` 会被记录并跳过 —— 这个租约不是本 Worker 的，所以它对该任务不作任何断言。任何其他异常都会变成 `dispatcher.fail(retryable=True)`：有限重试，然后终止。循环总会继续到下一个任务。

## 关闭

停止请求是协作式的。循环会停止租约新任务，并为进行中的步骤最多等待 `shutdown_grace_s`，该步骤的租约由心跳保活。如果步骤没能及时完成，循环会**放弃**它并返回。

放弃只用于进程关闭。Python 无法杀掉被放弃的步骤线程，所以它可能仍在运行、仍在写 EventLog：host 必须退出进程。一旦退出，租约就过期，下一次 `requeue_stale` 清扫会回收该任务。

## 横向扩展

多个进程只有在 **Postgres** 上才能排空同一个存储 —— 那里 append 在事务内针对存活租约做 fencing，租约过期跑在数据库时钟上。SQLite 没有跨主机 fencing —— 把它留在单主机上，那里多 Worker 池没问题。

就绪队列不做任何路由：一个 Worker 排空它租到的任何东西，所以一个存储里的每个任务都必须是那个池能跑的。给不同的工作负载画像各自的存储。

## 自己驱动循环

一个没有 `Client` 的 host —— 一个自行组装引擎、EventLog、ContentStore 和 Dispatcher 的独立排空进程 —— 可以直接构造这个排空原语。它期望的 `WorkerRuntime` 协议，以及每一个构造函数参数、方法和结果类型，见 [WorkerLoop 参考](../reference/worker-loop.md)。

## 另请参阅

- [WorkerLoop 参考](../reference/worker-loop.md) —— 完整的循环原语
- [唤醒与恢复](../concepts/wake-resume.md) —— Worker 实现的交付保证
- [已知限制](../operations/limitations.md) —— SQLite 的单主机边界和崩溃恢复范围
