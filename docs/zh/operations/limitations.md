# 已知限制

这些是代码有意不做的地方，原因通常是再往前做就要替 host 管它该自己管的事。它们都不是 bug；遇到故障请看[故障排查](troubleshooting.md)。

## 进程与部署

### 不会替你启动任何进程

- **限制：** `noeta-runtime` 和 `noeta-sdk` 都是库：没有 CLI、没有 HTTP/SSE 服务、没有调度守护进程。没有 worker 在跑时，入队的 Task 只会一直待在队列里。
- **办法：** 自己跑一个 `WorkerLoop`，或调用 `Client.start_workers(n)`。`examples/reference-host` 是最小的 host 示例，只用了公开接口。

### 多机部署需要 Postgres

- **限制：** 多个 worker **进程**共用一个数据库，只有 Postgres 是安全的（写事件和校验租约在同一个事务里，租约过期按数据库时钟算）。SQLite 和内存存储只能单机用，两个进程指向同一个 SQLite 文件是不安全的。
- **办法：** 跨机器就用 Postgres。单机上跑 worker 池没问题；同一进程里多个 client 也可以共用存储，每个 client 用自己的 `HostConfig.queue`，子任务沿用父任务的队列，worker 不会跨队列取活。本版修复：以前在同一个存储上再开一个 `Client`，它启动时的恢复会把另一个 `Client` 的后台子 agent 重跑一遍；现在恢复会跳过别的 client 还在跑的子 agent。参见 ADR：[多机租约隔离](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)、[worker 队列路由](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md)。

### Postgres：每个存储适配器一条连接，没有连接池

- **限制：** 事件日志、调度器、内容存储这几个 Postgres 适配器各自只用一条连接，外面套一把锁，所以同一个适配器上的调用是排队执行的。连接断了（数据库重启、空闲被踢、网络中断）会自动重连：单条语句重发一次；事务在发出 `COMMIT` 之前断开，就从头重跑整个事务。如果恰好断在 `COMMIT` 过程中，写入到底成没成功说不准，这时直接报错，不重试——带幂等键的事件写入除外，它重试是安全的。数据库一直起不来的话，调用照样失败。
- **办法：** 想要更高的数据库吞吐，就多开几个 worker 进程（每个进程有自己的连接）。参见 ADR：[多机租约隔离](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)。

## 持久性

### 崩溃恢复撤销不了副作用

- **限制：** 一步跑到一半被硬杀后，这次尝试会被标记为 `StepAttemptAbandoned`。只有当它记录下的所有操作本来都不需要审批时，才会自动重跑这一步；否则（或同一轮已经连续标记 3 次）Task 会被**搁置**：挂起，并附一条 `origin="system"` 的通知，逐条列出被打断的调用以及它是否完成。经人工批准的工具执行期间崩溃，一律搁置，重新挂回同一次审批。恢复不会悄悄重跑有副作用的调用，但已经做了的也撤销不了。
- **办法：** 打开被搁置的 Task，确认列出的操作到底执行了没有，然后发消息继续（这一轮从尝试开始前的状态重来），或重新批准。正常的 SIGTERM 不会触发这种情况。

### 关闭时可能留下一个还在跑的步骤

- **限制：** `stop()` 最多等 `shutdown_grace_s`，超时就放弃当前这一步。Python 杀不掉那个线程，它可能还在写事件日志。
- **办法：** 放弃之后直接退出进程。租约过期后 `requeue_stale()` 会把 Task 捡回来。`shutdown_grace_s=None`（或 `<= 0`）表示无限等待，卡死的步骤就只能 `kill -KILL <pid>`。

### 心跳续租有上限

- **限制：** 一步最多持有租约 `heartbeat_interval × heartbeat_max`（默认 360 次，实际是几个小时）。超过后租约被释放，下一次写入报 `InvalidLease`。
- **办法：** 遇到了就去检查这个 Task，别当成会自动恢复。

## 可观测性

### 可靠性事件只在本进程内

- **限制：** worker 的信号（`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`、`attempt_parked`、`cap_terminal_reconciled`、`dispatcher_unavailable`）默认只写结构化日志，不进事件日志，进程重启就没了。
- **办法：** 传一个 `reliability_sink`，把它们转发到你的监控系统。

### Task 等人回复时没人收到通知

- **限制：** Task 挂在 `HumanResponseReceived` 唤醒条件上，`answer` 负责送回复，但不会发 webhook、邮件或任何收件箱通知。
- **办法：** 订阅一个 `Observer`，把 `UserQuestionRequested` 转发到你自己的通知渠道，再用 `answer` 回复。

## 增长与成本

### 目录里没有的模型：保守压缩，价格为 0

- **限制：** 不认识的模型按 128,000 token 窗口、16,384 token 输出上限处理（压缩照常开启，但可能压得偏早），价格按 `0.0` 算，所以 `GovernanceState.cost` 一直是 0，`max_cost_usd` 永远不会触发。每项在日志里提示一次。
- **办法：** 通过 `HostConfig(extra_models={...})` 或 `noeta.sdk.providers` 的 `register_models` 注册一条 `ModelSpec`。

### 内容永远不会被回收

- **限制：** 内容存储按哈希寻址、只增不删，没有垃圾回收。`Client.delete_task` 会删掉整棵任务树的事件和调度状态，但保留内容块，因为它们可能按哈希被别的 Task 共用。树里还有 Task 持有租约时，它会返回 `reason="running"` 拒绝删除。
- **办法：** 按保留期规划存储容量，或自己写一个离线清理脚本，遍历剩余事件流引用到的内容。

## Sandbox

### 不提供容器创建

- **限制：** `SandboxProvider` 只是一个协议。自带的唯一实现是连接一个已经在跑的容器（来自 `SandboxExecEnvConfig`），`release` 什么也不做。创建和回收容器是 host 的事。
- **办法：** 自己实现 `SandboxProvider`，传给 `HostConfig.sandbox_provider`。`allocate` 返回一个 `SandboxHandle`；Task 恢复时，`attach` 按 `TaskHostBound` 上记录的 `exec_env_ref` 重新连上容器。见 [Sandbox](../guides/sandbox.md)。

### Sandbox 里的副作用不受租约约束

- **限制：** 对容器的调用走 HTTP，不在保护事件日志写入的 Postgres 事务里。一个已经丢了租约的 worker（GC 停顿、被 `SIGSTOP`）仍然能操作容器，属于"至少一次"，跟宿主机上跑了一半的 `Bash` 一样。影响范围只限于这个根任务自己的容器。
- **办法：** 没有自动办法，靠上面崩溃恢复那一套重跑和人工检查兜底。

### Sandbox 里被停下的 `Bash` 不带回输出

- **限制：** 每条前台命令在容器里有自己的 shell，中断、取消、关闭会话或者超时都会在容器里把它杀掉（2026-09-25 起）。这样停下的命令不会带回已经输出的内容，本地跑的则会带回停下前打印的部分。命令正常结束时，它放到后台的进程（`server &`）不会被杀，这点跟宿主机一样。
- **办法：** 输出多的命令把结果写到工作区的文件里，停下后用 `Read` 去看。

### 后台 shell 只能在宿主机用

- **限制：** `Bash(run_in_background=true)`（以及 `BashOutput` / `KillShell`）依赖宿主机的后台执行器，在 sandbox 里会直接报错。
- **办法：** 改成前台运行并给足 `timeout`，或者不在 sandbox 里跑。

### Sandbox 浏览器只看文字

- **限制：** 五个工具（`browser_navigate`、`browser_click`、`browser_type`、`browser_extract`、`browser_screenshot`）只有在容器里有可用浏览器、且启用了 `browser` 插件时才会出现。`browser_extract` 返回页面文字和编号的可交互元素；`browser_screenshot` 把 PNG 存进工作区，但不会给模型看。浏览器跟容器同生命周期、同成本。
- **办法：** 读内容用 `browser_extract`，不需要交互的页面用 `WebFetch`，截图留给人看。

## 不开放的扩展点

### 不能替换上下文组装器

- **限制：** 替换 `ContextComposer` 会破坏 provider 缓存依赖的固定提示词前缀。只开放只增不改的钩子：`ContentKindSpec` 常驻内容，或组装时的 `reminder`。
- **办法：** 用这两个钩子，或者通过 `policy` 插件点替换 `Policy`。见[上下文](../how-it-works/context.md)。

## 下一步

- [故障排查](troubleshooting.md)
- [工作原理](../how-it-works/index.md)
- [`WorkerLoop` 参考](../reference/worker-loop.md)
