# 已知限制

已发布代码能力的边界。每一条说明边界是什么、你何时会遇到它，以及有变通方案时的变通方案。这些都不是 bug——它们是设计停下来的地方。

## 库不会替你运行任何进程

**含义：** `noeta-runtime` 和 `noeta-sdk` 是库。没有 CLI、没有 console script、没有 HTTP 或 SSE server，也没有调度守护进程。排空循环以原语 `noeta.runtime.worker.WorkerLoop` 的形式提供；由 host 构造并运行它（或调用 `Client.start_workers(n)` 获得一个常驻池）。没有任何东西替你启动它，因此在没有 Worker 运行时入队的任务只会停在就绪队列里。

**何时遇到：** 你以为存在 `noeta run`，或者你把工作入队后什么都没推进。

**变通方案：** 把这些库嵌入你自己的 host。`examples/reference-host` 是最小的一个，完全由公开面组装而成。

## 多主机协调需要 Postgres

**含义：** 支持单主机多 Worker——一个进程运行一个常驻 `WorkerLoop` 池，多个任务的轮次同时推进。多个*主机进程*共享一个数据库仅在 **Postgres** 上受支持：事件追加在事务中针对活跃 lease 进行 fencing，lease 过期按数据库时钟计算（因此每主机的时钟偏差不会造成脑裂），并有一个 `worker_id` 列记录持有者。**SQLite** 和**内存**后端是单主机的——它们没有跨主机 fencing，因此把两个主机进程指向同一个 SQLite 文件是不安全的。

**何时遇到：** 你想让多台机器上的 Worker 进程排空一个共享存储。

**变通方案：** 多主机部署使用 Postgres 后端。在 SQLite 上，保持单主机（该主机上的多 Worker 池没问题），或给不同的工作负载配置各自的 SQLite 文件——就绪队列中没有跨存储路由，因此一个存储中的任务对排空另一个存储的 Worker 不可见。

参见 [ADR：多主机 lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)。

## 崩溃恢复不会撤销副作用

**含义：** Worker 在 Step 中途崩溃（`kill -KILL`、断电）会在下一次 lease 时恢复：被中断的 Attempt 会以一个持久的 `StepAttemptAbandoned` 标记密封，并且当该 Attempt 记录的所有内容都无需经过人工审批门就能运行时，Step 会被重新驱动。当 Attempt 存在无法证明的副作用时——或在一个轮次里连续密封三次后——任务改为被**停放**：作为一个已停止的对话挂起，附带一条 `origin="system"` 通知，逐一列出被中断的调用以及它是否完成。在人工审批的工具执行期间崩溃总是停放，并在同一个审批上重新挂起。恢复从不静默终止任务，也从不静默重新运行有副作用的调用——但它同样无法撤销崩溃的 Attempt 已经做过的任何事。

**何时遇到：** 硬杀发生在一个已经运行过有副作用工具的 Attempt 期间。正常的 SIGTERM 关闭不会触发它，读取或规划期间的崩溃则无需任何人参与即可恢复。

**变通方案：** 打开被停放的对话——通知列出了被中断的内容。核实这些操作是完全、部分还是完全没有应用，然后输入内容以继续（轮次从干净的 Attempt 前基线恢复），或重新审批待处理的调用。

## 关闭可能放弃一个仍在运行的 Step

**含义：** 在 `stop()` 时，`WorkerLoop` 最多等待 `shutdown_grace_s` 让进行中的 Step 完成。如果它没有完成，循环会**放弃**该 Step 并返回——但 Python 无法杀死被放弃的线程。它可能仍在运行并写入 EventLog。

**何时遇到：** 某个 Step 挂起（比如一次对无响应外部 API 的工具调用），且宽限窗口到期。

**变通方案：** **退出进程。** 放弃之后，host 必须调用 `sys.exit()` 或等效操作。被放弃的线程随进程一同死亡，它的 lease 过期，`requeue_stale()` 在下一次启动时回收该任务。`shutdown_grace_s=None`（或 `<= 0`）会无限等待——这时卡住的 Step 需要一个外部的 `kill -KILL <pid>`。

## 心跳保活窗口有上限

**含义：** 心跳让慢 Step 的 lease 保持存活，但不是永远。Dispatcher 把心跳延长次数限制在 `heartbeat_max`（默认 360），因此 `heartbeat_interval × heartbeat_max` 就是一个 Step 能持有 lease 的最长时间。超过上限后，lease 被强制释放，该 Step 下一次写 EventLog 会以 `InvalidLease` 失败。

**何时遇到：** 单个 Step——一次模型轮次加上它所有的工具调用——耗时超过上限窗口。在默认值下这是数小时，所以很少见。

**变通方案：** 把命中上限当作一个运维故障信号，而不是恢复路径。循环会记录它并继续下一个任务，但命中上限的任务可能需要检查：看它是否仍然可行，还是应该关闭。

## 可靠性事件是进程本地的

**含义：** Worker 会发出 `ReliabilityEvent`——`stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`、`attempt_parked`——到一个可注入的接收器，默认是结构化日志。它们**不是** EventLog 事件，不会持久化，也不会在重启后存活。

**何时遇到：** 你在 Worker 可靠性信号之上构建监控或告警。

**变通方案：** 挂载一个自定义的 `reliability_sink`，把它们转发到你的监控系统。每个事件都以 Worker 能从 dispatcher seam 证明的东西命名——比如 `heartbeat_invalid_lease` 是一个症状，其原因可能是上限、过期或一次 requeue。

## 没有任何东西通知有人任务正在等待人类

**含义：** 人机协作在带内完全接通：Engine 在一个 `HumanResponseReceived` 唤醒条件上挂起，`answer` 客户端动词递送响应。没有带外通道——任务开始等待时，没有 webhook、没有邮件、没有跨任务收件箱会触发。

**何时遇到：** Agent 在没有人驱动任务时提出一个问题。任务会持久地等待（这正是重点），但没有任何东西通知任何人。

**变通方案：** 交互式地驱动任务，或者给 EventLog 订阅一个 `Observer`，把 `UserQuestionRequested` 事件转发到你自己的通知通道，并用 `answer` 递送回复。

## 未登记的模型会静默关闭 compaction 与定价

**含义：** compaction 旋钮和成本都源自 `providers` 内置插件里的模型目录。对于目录未描述的模型，`derive_compaction_config` 返回 `COMPACTION_OFF`——上下文 compaction 从不启动，因此一段长对话会一直运行到 provider 自己拒绝请求为止。定价以同样的方式退化：一个未定价的模型每次往返花费 `0.0`，因此 `GovernanceState.cost` 始终为零，`max_cost_usd` 预算永远不会触发。这两种退化都不会抛出异常。

**何时遇到：** 你把 `Options.model` 指向一个网关模型 id、一个微调模型，或一个不在 `CATALOG` 里的自托管模型。

**变通方案：** 为它添加一行 `ModelSpec`。`CATALOG` 和 `ModelSpec` 从 `noeta.sdk.providers` 重新导出；一行提供 `context_window`、`max_output_tokens` 和价格字段，这就是两处推导所读取的全部内容。

## 内容永远不会被垃圾回收

**含义：** ContentStore 是内容寻址且仅追加的，且不附带任何 GC。`Client.delete_task` 会清除一个任务在整个子任务树上的事件流和 dispatcher 状态，但刻意保留内容 blob——它们按哈希在任务之间共享，因此删除一个任务无法证明某个内容体不可达。于是存储会随着记录的工具输出、snapshot 和 compaction 摘要单调增长。

**何时遇到：** 一个带有大量工具输出的长期部署。

**变通方案：** 库内没有。为保留量确定存储容量，或针对你的后端写一个离线清扫，遍历剩余各流的引用。当树中任何任务持有活跃 lease 时，`delete_task` 还会以 `reason="running"` 拒绝，因此清除永远不会与进行中的轮次竞争。

## 库不附带沙箱置备器

**含义：** `SandboxProvider` 是 SDK 定义并驱动（通过 `SandboxExecEnvManager`）的一个协议，而不是它附带的实现。盒子里唯一的 provider 把一个 `SandboxExecEnvConfig` 适配成一个**仅附着**的 provider：它连接到一个已经在运行的容器，其 `release` 是空操作，因为它并不拥有该容器。置备与回收——运行 `docker`、调用 K8s API、选择挂载——属于 host。

**何时遇到：** 你期望开箱即用地为每个对话得到一个全新容器。

**变通方案：** 在你的 host 里实现 `SandboxProvider`，并把它作为 `HostConfig.sandbox_provider` 传入。`allocate` 返回一个携带寻址信息以及一个活跃 `SandboxAuth` 策略的 `SandboxHandle`；`attach` 重新连接到记录在 `TaskHostBound` 上的 `exec_env_ref`，这就是一个被恢复或被回收的会话重新找到它的容器的方式。这个重连能否跨机器工作，取决于你写的 provider 的性质，而不是 SDK。

## 沙箱副作用在 Worker 代际之间不受 fencing

**含义：** 当一个会话在沙箱容器里运行时，它的文件和 shell 副作用通过 HTTP 到达容器——在为 EventLog 写入做 fencing 的共享 Postgres 事务之外。一个被 fencing 挡在日志外的 Worker（一次 GC 暂停、一次 `SIGSTOP` 后又复活）仍然可以向容器 `POST`。因此沙箱副作用是至少一次且不受 fencing 的，与 host 上运行了一半的 `shell_run` 属于同一类：一个回收任务的 Worker 会重连到同一个容器并重新驱动该 Step，但一个缓慢的僵尸在此期间可能污染容器。因为一个容器属于一棵根任务树，僵尸只会污染它自己的会话。

**何时遇到：** 一个持有沙箱会话的 Worker 停滞得足够久，以致它的 lease 过期、另一个 Worker 回收了该任务，然后它醒来又向容器发出一次调用。

**变通方案：** 没有自动方案。它受与上面覆盖崩溃 Step 副作用相同的那套 Step-Attempt 重新驱动和人工审查所约束。

## 沙箱 `shell_run` 没有远程硬杀

**含义：** 在 host 上，`shell_run` 的 `timeout` 映射到一个真正杀死进程的子进程超时。在沙箱下没有远程取消动词，因此超时由那一次 HTTP 调用的读取超时在*客户端侧*强制执行。你传入的 `timeout` 会被遵守——一个运行超时的命令会以请求的预算作为超时运行报告给模型——但该命令在调用返回后**仍在容器里继续运行**。它的副作用可能在工具已经报告超时之后才落地。

**何时遇到：** 一个沙箱 `shell_run`，其命令超过了它的 `timeout`——比如一个挂起的构建或测试运行。

**变通方案：** 把超时的沙箱 `shell_run` 当作"可能仍在运行"；一条后续命令可以观察或清理它的部分效果。给真正长时间运行的命令一个明确更大的 `timeout`，这样客户端就不会过早切断调用。

## 后台 shell 仅限 host

**含义：** `shell_run(run_in_background=true)` 把校验过的 argv 交给 host 的后台执行器，并返回一个作业 id，`shell_poll` 和 `shell_kill` 随后用它来寻址。一个沙箱 `ExecEnv` 会报告它不支持后台执行，工具会返回一个错误，而不是在前台运行该命令。

**何时遇到：** 一个沙箱化的 Agent 试图启动一个长时间运行的服务器或监视器。

**变通方案：** 用一个宽裕的 `timeout` 在前台运行命令，或在后台作业不可或缺时把会话放到沙箱之外运行。

## 沙箱浏览器是文本级且限定于容器的

**含义：** 一个沙箱会话可以通过五个 Noeta 自有工具（`browser_navigate`、`browser_click`、`browser_type`、`browser_extract`、`browser_screenshot`）驱动容器的无头浏览器。三个边界：

- **没有容器就没有浏览器。** 只有当会话的后端袋里有一个活跃的浏览器后端*并且* Agent 激活了 `browser` 时，这个 pack 才会挂载。否则工具集与非浏览器会话逐字节相同。
- **感知是文本和元素级的，而非视觉的。** `browser_extract` 返回页面文本，加上一个带编号的可交互元素列表，模型按索引点击和输入。`browser_screenshot` 把一张 PNG 保存为工作区制品；它**不会**作为视觉反馈给模型，因此需要视觉理解的页面无法被完整处理。
- **浏览器与容器共存。** 它共享会话容器的生命周期和成本；没有单独的暂停。

**何时遇到：** 一个必须读取仅以像素渲染的图表的任务，或一个需要在没有容器的情况下浏览的任务。

**变通方案：** 内容优先用 `browser_extract`，无需交互的页面用 `webfetch`；需要人来看时用 `browser_screenshot`。

## composer 无法被替换

**含义：** `ContextComposer` 是用户面上一个封闭的扩展点。稳定前缀的 KV-cache 可复现性是一条硬约束，因此不提供整体替换 composer。开放的钩子是仅注册且仅追加的：一个 `ContentKindSpec`（一个半稳定的常驻项）或一个 compose 时的 `reminder`（动态后缀尾部）。两者都不触碰稳定前缀。

**何时遇到：** 你想要一个根本不同的提示布局。

**变通方案：** 通过开放的面添加常驻项和 reminder，或者把这个决策移到一个自定义 `Policy` 里——`Policy` 确实可以通过 `policy` 面替换。

## 另见

- [故障排查](troubleshooting.md)——症状 → 原因 → 解决方案
- [唤醒与恢复](../concepts/wake-resume.md)——传递保证及其范围
- [WorkerLoop 参考](../reference/worker-loop.md)——构造函数旋钮与关闭行为
- [架构概览](../architecture/overview.md)——完整的系统图景
