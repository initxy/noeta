# 部署 Worker

本指南教你运行一个常驻的 worker 池，持续排空一个持久化存储，好让 Task 在创建它们的那次请求消失之后仍然继续推进。你需要[你的第一个 agent](../tutorials/first-agent.md) 里的 SDK 基础。

## 你为什么需要它

只有当某个东西从 Dispatcher 的就绪队列里租走一个 Task，它才会推进。没有任何东西替你启动这次排空。没有运行中的 worker 时：

- `wait_timer` 挂起永远不会唤醒 —— worker 的定时器轮询是 `TimerFired` 的唯一生产者；
- 某个 worker 在半途崩溃的 Task 永远不会被回收，因为过期 lease 的清扫跑在排空循环内部；
- 一个进程置为就绪的 Task 永远不会被另一个进程接走。

worker 是任何"必须活得比创建它的请求更久"的东西的部署形态。

## 1. 使用持久化存储

跨进程交接只能通过共享的落盘状态完成，因此常驻池需要真正的存储。`HostConfig.storage_path` 接收一个字符串 —— 一个 SQLite 文件路径、一个 `postgresql://` DSN，或 `":memory:"` —— 并按正确的顺序构建整个 `(EventLog, ContentStore, Dispatcher)` 三元组：

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

不要给常驻池用 `":memory:"` —— 存储会随进程一起消亡，别的东西谁也看不见它。

如果你自己构建这个三元组，请用 `noeta.sdk.storage`，并让三个组件落在同一个数据库上；EventLog 会把 Dispatcher 当作它的 lease 校验器：

```python
from noeta.sdk.storage import build_storage_stack

event_log, content_store, dispatcher = build_storage_stack(
    "sqlite", path="./noeta.sqlite",
)
```

`build_storage_stack` 接受 `"memory"`（无需配置）、`"sqlite"`（`path=`）和 `"postgres"`（`dsn=`）。`open_storage_stack` 走的是 `HostConfig.storage_path` 所用的同一套按值形状的分派。把三元组作为 `HostConfig` 的 `event_log` / `content_store` / `dispatcher` 字段传入 —— 三个都给或都不给；把它们和 `storage_path` 混用会抛 `ValueError`。

## 2. 启动和停止这个池

```python
with client:
    client.start_workers(4)
    ...
    stopped = client.stop_workers(timeout=30.0)
    print("all workers exited:", stopped)
```

```
all workers exited: True
```

每个 worker 跑在自己的守护线程上，有自己的 `worker_id`，而它们全都排空同一个就绪队列。并发的 worker 是安全的：每一次经 lease 校验的追加都有围栏，所以一个 lease 已被回收的 worker 会被拒绝，而不是被允许写入。

`start_workers` 是一次性的 —— 第二次调用会抛 `RuntimeError`。`stop_workers` 向每个循环发出信号并 join 线程，当所有线程都在 `timeout` 内退出时返回 `True`。超时时它返回 `False`，并刻意继续跟踪这个池，好让一次重试能把活干完，而不是在旧池上再叠一个新池。`Client.shutdown`（因而也包括离开 `with` 块）会在拆卸其他任何东西之前先停掉这个池。

## 3. 调整各项参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `num_workers` | `1` | 排空线程数。必须 `>= 1`。 |
| `poll_interval` | `0.1` | 就绪队列为空时的休眠时间 |
| `heartbeat_interval` | `30.0` | 每步的 lease 保活 |
| `stale_sweep_interval` | `10.0` | `requeue_stale()` 的节奏 |
| `timer_poll_interval` | `1.0` | `fire_due_timers()` 的节奏 |
| `lease_seconds` | `600.0` | 每个 Task 的初始 lease 期限 |
| `shutdown_grace_s` | `10.0` | 停止后对进行中 Step 的最长等待。`None` = 无限等待 |

## 一个 worker 每轮迭代做什么

1. 清扫过期 lease —— `requeue_stale()` 回收 lease 已过期的 Task。
2. 轮询定时器 —— `fire_due_timers()` 把每个到期的 `wait_timer` 挂起用一次 `TimerFired` 唤醒翻回就绪。
3. 租下一个就绪的 Task 并把它推进一步。
4. 如果队列是空的，就休眠 `poll_interval`。

一个有毒的 Task 绝不会让循环崩溃。`InvalidLease` 会被记录并跳过 —— 这个 lease 不属于本 worker，所以它对该 Task 不作任何断言。其他任何异常都会变成 `dispatcher.fail(retryable=True)`：有界重试，然后终态。循环总是继续处理下一个 Task。

## 关闭

停止请求是协作式的。循环会停止租用新 Task，并为进行中的 Step 最多等待 `shutdown_grace_s`，其间由心跳维持该 Step 的 lease。如果这个 Step 没能及时完成，循环会**放弃**它并返回。

放弃只用于进程关闭。Python 无法杀掉被放弃的 Step 线程，因此它可能仍在运行并写入 EventLog：host 必须退出进程。一旦退出，lease 就会过期，下一次 `requeue_stale` 清扫会回收该 Task。

## 横向扩展

多个进程只有在 **Postgres** 上才可以排空同一个存储 —— 那里追加会在事务内对着活跃 lease 加围栏，而 lease 过期按数据库时钟计算。SQLite 没有跨主机围栏 —— 把它限制在单台主机上，在那台主机上跑一个多 worker 的池是没问题的。

就绪队列不做任何路由：worker 排空它租到的任何东西，所以一个存储里的每个 Task 都必须是那个池跑得了的。给不同的工作负载画像各自的存储。

## 自己驱动这个循环

没有 `Client` 的 host —— 一个自行组装引擎、EventLog、ContentStore 和 Dispatcher 的独立排空进程 —— 可以直接构造这个排空原语。它期望的 `WorkerRuntime` 协议，以及每一个构造函数参数、方法和结果类型，见 [WorkerLoop 参考](../reference/worker-loop.md)。

## 下一步

- [WorkerLoop 参考](../reference/worker-loop.md) —— 完整的循环原语
- [用 Docker 部署](docker-deployment.md) —— 把这个池打包成镜像
- [唤醒与恢复](../concepts/wake-resume.md) —— worker 所实现的投递保证
- [已知限制](../operations/limitations.md) —— SQLite 的单主机边界与崩溃恢复的范围
