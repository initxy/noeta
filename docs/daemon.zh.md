# 常驻排空（`WorkerLoop` 原语） { #resident-drain-the-workerloop-primitive }

> **单主机预览。** 常驻 worker 循环用于本地和单主机使用。它对其限制是诚实的：唤醒传递现在是**持久恰好一次**（单 worker；H2 / [ADR：子任务扇出和持久唤醒](adr/subtask-fanout-and-durable-wake.md)），但它仍然有一个**有界进程关闭**，放弃（不中断）超过其关闭宽限期的卡住步骤，一个有界的每步租约 keepalive 窗口，以及一个**单 worker** 且无并发（多 worker 是单独的未来切片）。崩溃恢复仅对无孤儿事件类是字节相等的。在依赖它做任何重要事情之前，请阅读[限制](#limitations)。

> **没有发布的 CLI。** TL6 移除了 `noeta serve` 命令（以及每个其他 `noeta <subcommand>`）；任何包中都**没有控制台脚本**。常驻排空循环现在是**库原语** `noeta.runtime.worker.WorkerLoop`。嵌入器构造并运行它；发行版中没有任何东西为你启动它。聊天**服务器**（`noeta serve --ui` 曾经覆盖的 UI 用例）现在是单独的启动器 `python -m noeta.agent`（见[聊天服务器](#the-chat-server)）。

## 它是什么 { #what-it-is }

通过聊天服务器（HTTP `POST /tasks`，见[聊天服务器](#the-chat-server)）启动的运行是**一次性**的：它驱动一个任务一次并返回。`WorkerLoop` 是**常驻**等价物。它运行一个连续循环：

1. 从 dispatcher 租赁下一个就绪 Task，
2. 驱动它一步（3 状态机——唤醒 / 排空 / 挂起跳过——由 `noeta.runtime.worker.run_leased_task` 实现），
3. 释放租约，以及
4. 定期回收被崩溃 worker 留下的过期租约。

`WorkerLoop` 位于 L2 运行时层（`packages/noeta-runtime/noeta/runtime/worker.py`），因此嵌入或 SDK 可以运行相同的排空循环而不依赖任何更高层。它驱动满足窄 `WorkerRuntime` 结构 Protocol 的任何对象——`engine` / `event_log` / `content_store` / `dispatcher`。仓库内的 `noeta.testing.profile.RuntimeBundle`（由 `noeta.testing.profile.build_runtime` 返回）满足它，这是站立一个的最简单方法：

```python
from noeta.runtime.worker import WorkerLoop

# rt is any WorkerRuntime: engine / event_log / content_store / dispatcher.
# noeta.testing.profile.build_runtime(...) returns a RuntimeBundle that
# satisfies it; a real embedder supplies its own wired runtime.
loop = WorkerLoop(
    rt,
    worker_id="noeta-worker",
    lease_seconds=600.0,
    poll_interval=0.5,
    heartbeat_interval=30.0,
    stale_sweep_interval=10.0,
    shutdown_grace_s=30.0,
)

# Blocks until stop() is called. install_signals=True wires SIGTERM /
# SIGINT to loop.stop() for the duration (main thread only) and restores
# the previous handlers on exit.
loop.run_forever(install_signals=True)
```

`run_forever(install_signals=True)` 是常驻形式。如果你自己接线信号，使用 `noeta.runtime.worker.install_stop_signals(loop)`（它返回一个恢复 callable）并调用 `loop.run_forever()` 不带标志。要从另一个线程停止，调用 `loop.stop()`。

**一个循环 = 一个 profile。** `WorkerLoop` 驱动它构造时使用的任何单个 `WorkerRuntime`，因此它恰好绑定一个 provider / 模型 / 工具集 / 策略（除非运行时提供每任务 `resolve_engine(task)` seam——[ADR：代理身份和来源](adr/agent-identity-and-provenance.md)——在这种情况下它用自己的 Agent Engine 驱动每个任务）。使用单 Engine 运行时没有每任务 provider 或模型解析：循环拾取的每个任务都用运行时构建时使用的 profile 驱动。

这有一个尖锐的后果：**给定存储中的每个任务必须与排空它的循环兼容。** Dispatcher 交出一个没有任务路由的共享就绪队列，因此循环将租赁和驱动*任何*就绪任务——包括为不同预期 profile 入队的任务。要运行不同 profiles，给每个自己的 **sqlite 文件**（或使用外部队列分区工作）；不要将两个不同配置的循环指向同一个存储。同存储路由 / 每任务 profile 解析器是未来的工作，多 worker 并发也是（见[限制](#limitations)）。

## 存储：跨进程入队的真实文件 { #storage-a-real-file-for-cross-process-enqueue }

常驻排空循环存在以托管由*其他*进程（SDK、操作员脚本、其他地方的聊天服务器）入队的任务，跨进程入队仅通过共享磁盘存储工作。在**真实 sqlite 文件**上构建循环的运行时，以便从任何进程对 `./state.sqlite` 入队的任务被同一文件上的运行循环拾取——无需重启。

`:memory:` 被接受但是**仅用于开发/测试**：内存栈是创建它的进程私有的，因此没有其他东西可以入队到其中。仅将其用于循环本身的冒烟测试。

## 旋钮 { #knobs }

`WorkerLoop` 的行为由其构造函数参数设置（没有标志接口——它是一个库对象）。相关的：

| 构造函数参数 | 默认值 | 含义 |
| --- | --- | --- |
| `rt` | *(必需)* | 要驱动的 `WorkerRuntime`（绑定单个 profile）。 |
| `worker_id` | `"noeta-worker"` | 租约所有者 id。 |
| `lease_seconds` | `600.0` | 每个任务授予的初始租约截止时间。 |
| `poll_interval` | `0.5` | 就绪队列为空时休眠的秒数。 |
| `heartbeat_interval` | `30.0` | 每步心跳扩展慢步骤租约的频率（`<= 0` 禁用心跳）。 |
| `stale_sweep_interval` | `10.0` | `requeue_stale` 扫描之间的间隔（`<= 0` 禁用扫描）。 |
| `shutdown_grace_s` | `30.0` | 停止时，在**放弃**飞行步骤之前等待的最大秒数（H1）。`None` / `<= 0` = 旧的无界等待。 |
| `reliability_sink` | 结构化日志 | 进程本地 `ReliabilityEvent` 去哪里。 |

**没有 `workers` 旋钮。** 循环在此预览中设计为单 worker（见[限制](#limitations)）。

## 聊天服务器 { #the-chat-server }

`noeta serve --ui` 曾经覆盖的 UI 用例现在是单独的启动器 **`python -m noeta.agent`**（来源：`apps/noeta-agent/noeta/agent/__main__.py` 和 `backend/lifecycle.py`）。它**不是** argparse CLI，接受**零位置参数**：它从环境（或通过 `noeta.agent.backend.lifecycle.BackendConfig.from_env` 的 `NOETA_AGENT_CONFIG` JSON 文件）读取配置，启动 HTTP/SSE 聊天服务器加上捆绑的 Web SPA，打印服务 URL，并阻塞直到 SIGINT / SIGTERM。它总是服务 UI——没有 **`--ui` / `--serve` 标志**（那些是 `noeta serve` 标志，已经消失了）。

```bash
# the env-configured launcher — equivalent of the old "noeta serve --ui"
NOETA_AGENT_PROVIDER=stub \
NOETA_AGENT_SQLITE=./state.sqlite \
python -m noeta.agent
```

配置完全从环境读取（括号中为默认值）：`NOETA_AGENT_PROVIDER`（`stub`）、`NOETA_AGENT_SQLITE`（未设置 = 内存）、`NOETA_AGENT_PORT`（`0` = 临时）、`NOETA_AGENT_HOST`（`127.0.0.1`）、`NOETA_AGENT_MODEL`（`stub-model`）、`NOETA_AGENT_WORKSPACE`（cwd）、可选的 `NOETA_AGENT_API_KEY` / `NOETA_AGENT_BASE_URL`，或指向具有相同键的 JSON 文件的 `NOETA_AGENT_CONFIG`。

第一行 stdout 是服务 URL（`noeta.agent serving at http://127.0.0.1:<port>/`）。打开它以查看队列并使用 Resume 表面重新驱动单个任务。请注意，此启动器服务 UI 和 HTTP 命令表面；它启动**无** `WorkerLoop`（委派关闭，因此聊天任务仅在人类手柄上挂起，内联驱动程序同步解析——没有什么可排空的）。如果你需要常驻排空循环，构造一个 `WorkerLoop` 如上所示——它们是独立的关注点。

## 生命周期 { #lifecycle }

* **连续排空** —— 每次迭代租赁一个就绪任务并运行单步（`WorkerLoop.tick()`）。当队列为空时，循环休眠 `poll_interval` 秒，然后重试。
* **定期过期扫描** —— 每 `stale_sweep_interval` 秒循环运行 `requeue_stale()`（`WorkerLoop.maybe_sweep()`），将截止时间已过的租约（例如崩溃的 worker）返回到就绪队列。
* **每步心跳** —— 当单步运行时，侧线程每 `heartbeat_interval` 秒扩展该步骤的租约，因此合法慢的步骤不会在飞行中被回收。
* **Worker 异常策略** —— 常驻循环不得因一个中毒任务而崩溃。如果步骤引发异常，循环将租约失败为可重试（有界重试，然后终止）并继续；如果租约已经丢失（`InvalidLease`），它记录并继续，而不声称关于任务状态的任何事情。

## 关闭 { #shutdown }

`loop.stop()`（`install_signals=True` 将其接线到 SIGTERM / SIGINT）触发**有界进程关闭**（H1）：循环停止租赁新任务并等待最多 `shutdown_grace_s` 秒让飞行步骤完成（其租约由心跳保持活动）。如果它及时完成，它正常释放并且循环返回。如果宽限期过去，循环**放弃**该步骤——停止其心跳（因此租约将过期），发出 `shutdown_abandoned` 可靠性事件，设置 `WorkerLoop.abandoned`，并在不接触租约的情况下返回。

```python
# the resident equivalent of Ctrl+C / kill -TERM: another thread, a
# signal handler, or install_signals=True flips the running flag.
loop.stop()
```

**仅进程关闭——不是安全的嵌入内继续。** Python 不能中断被放弃的步骤线程；它可能仍在运行并且可能仍在写入 EventLog。因此放弃仅在**进程退出**时是安全的：被放弃的线程随之死亡，租约然后过期，`requeue_stale` 在下一次启动时回收任务。在 `WorkerLoop.abandoned` 被设置后在进程内重用相同的运行时/循环是**不支持的**——主机必须退出进程。`shutdown_grace_s=None` / `<= 0` 恢复旧的无界等待。见[限制](#limitations)。

## 限制 { #limitations }

这些是单主机预览的故意边界，不是 bug。

### 持久恰好一次唤醒（H2） { #durable-exactly-once-wake-h2 }

挂起任务的唤醒被传递和消费**恰好一次，即使在崩溃后也是如此**（[ADR：子任务扇出和持久唤醒](adr/subtask-fanout-and-durable-wake.md)）。匹配的唤醒**在 `lease()` 中存活**（它不再在租约时被销毁）；它仅由呈现其消费的唤醒的**消费释放**清除（在持久 `TaskWoken` 写入之后），否则在崩溃后由 `requeue_stale` **重新传递**。Worker 的唤醒分支是一个恢复状态机，以当前 suspend-window 内最新的匹配 `TaskWoken` 为键，因此在已经写入 `TaskWoken` 的崩溃后的重新传递被协调（终止 / 重新挂起 / 继续）**而没有第二个 `TaskWoken`**。

净效果：*应该触发的唤醒总是触发；重新传递的唤醒仅被消费一次。* 不需要操作员重新发出。这是**单主机 / 单 worker** 恰好一次——多 worker 并发（以及它暗示的完成排序 / fencing）是未来的切片。在**飞行中途**崩溃的步骤（在 `TaskWoken` 之后，部分步骤事件，仍然运行）仍然是下面记录的 **partial-step-orphan** 限制——H2 不会静默重新运行部分步骤。

### 关闭——有界，但仍然没有进程内中断 { #shutdown--bounded-but-still-no-in-process-interrupt }

在 `stop()`（SIGTERM / SIGINT，或直接调用）时，循环停止租赁并等待最多 `shutdown_grace_s` 秒让飞行步骤，然后**放弃**它并返回（H1）。它仍然**不**中断运行步骤（Python 不能杀死线程）——放弃是**进程关闭**：主机必须退出，被放弃的线程随之死亡，其租约过期，`requeue_stale` 在下一次启动时回收任务。因此卡住的步骤不再永远保持循环，但飞行尝试**没有**干净地完成。见[`failure-modes.md`](failure-modes.md)。

### 崩溃恢复限于无孤儿事件类 { #crash-recovery-is-scoped-to-the-no-orphan-event-class }

在**写入任何持久步骤事件之前**死亡的 worker（仅租约 / `TaskStarted` 之前）是完全可恢复的：`requeue_stale` 返回任务，新 worker 通过 `fold` 重建它并驱动它完成——记录与无崩溃运行字节相等。**留下孤儿事件的部分步骤崩溃**（进程在 `ContextPlanComposed` / `LLMRequestStarted` / `ToolCallStarted` 之后但在配对的完成事件之前死亡）是**已知限制**：`fold` 可能重建状态，但从头重放不会重现孤儿尝试。关闭这需要尝试日志 / 重放语义机制（它自己的 ADR）——它**不**在此处解决。

### 可靠性事件是进程本地的（不是 EventLog） { #reliability-events-are-process-local-not-the-eventlog }

Worker 发出进程本地 `ReliabilityEvent`——`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`——到可注入的 sink（`reliability_sink`；默认：结构化日志）。这些**不是** EventLog 事件，**不**被持久化或重放，每个都以 worker 可以从 dispatcher seam 证明的内容命名（例如 `heartbeat_invalid_lease` 是症状——原因可能是上限 / 过期 / 重新入队 / 释放；`step_failed_retryable` 意味着 worker 调用了 `fail(retryable=True)`，不是任务进入终止）。

### 心跳 keepalive 窗口 { #heartbeat-keepalive-window }

心跳保持慢步骤的租约活动，但不是永远。Dispatcher 限制心跳扩展次数（`heartbeat_max`），因此 `heartbeat_interval × heartbeat_max` 是单步可以持有租约的最大时间。超过该上限，租约被强制释放，步骤的下一次 EventLog 写入失败并显示 `InvalidLease`。**此上限命中是操作故障路径，不是恢复路径**——3A 不添加自动上限命中恢复；它可能需要操作员检查。

### 单 worker { #single-worker }

循环运行一个 worker。没有进程内并发，没有 `workers` 旋钮。吞吐量是一次一步。多 worker / 多主机协调不在此预览范围内。

## 另见 { #see-also }

* [`failure-modes.md`](failure-modes.md) —— 上述限制的恢复配方。
* [`noeta-agent.md`](noeta-agent.md) —— `python -m noeta.agent` 编码代理及其 HTTP 表面（本地聊天/trace Web UI）。
* [`concepts.md`](concepts.md) —— 排空循环位于其上的租约 / dispatcher / Task 模型。

> **Trace 导出**是一个库 observer，不是 shell 命令：`noeta.observers.trace_export.make_jsonl_trace_observer(event_log=..., path=...)`，由嵌入器接线（例如 `noeta.testing.profile.build_runtime(trace_file=...)`）。没有 verify/replay 命令、HTTP endpoint 或 `noeta.verify` API：重新推导任务状态只是对其 EventLog 的 `fold`（无 provider 重新调用），重新驱动一个是 `noeta.runtime.worker.run_leased_task`（聊天服务器的一次性驱动也使用的库原语）。
