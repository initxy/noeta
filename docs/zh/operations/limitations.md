# 已知限制

已发布代码能力的边界。每一条都说明边界是什么、什么时候会撞上它，以及如果有的话，绕过的办法是什么。

**这些都不是 bug。** 它们是设计刻意停下的地方 —— 通常是因为再往前走就需要库去拥有本该由 host 拥有的东西。如果你追的是一个故障，请从[故障排查](troubleshooting.md)开始。

六组：
[库不会替你运行的东西](#库不会替你运行的东西) ·
[持久性](#持久性边界) ·
[可观测性](#可观测性缺口) ·
[增长与成本](#增长与成本) ·
[sandbox](#sandbox-边界) ·
[封闭的扩展点](#封闭的扩展点)。

## 库不会替你运行的东西

### 这两个库不替你跑任何进程

**这意味着：** `noeta-runtime` 和 `noeta-sdk` 是库。没有 CLI、没有 console script、没有 HTTP 或 SSE server，也没有调度守护进程。排空循环以原语 `noeta.runtime.worker.WorkerLoop` 的形式发布；由 host 构造并运行它（或者调 `Client.start_workers(n)` 得到一个常驻池）。没有任何东西替你启动它，所以一个在没有 worker 运行时入队的 Task，就只是待在就绪队列里。

**什么时候撞上：** 你以为存在一个 `noeta run`，或者你把工作入了队却什么都没推进。

**绕过办法：** 把这两个库嵌进你自己的 host。`examples/reference-host` 是最小的那种，完全由公共面组装而成。

## 持久性边界

### 多主机协调需要 Postgres

**这意味着：** 单主机多 worker 是支持的 —— 一个进程跑一个常驻 `WorkerLoop` 池，多个 Task 的轮次同时推进。多个 *host 进程*共享一个数据库只在 **Postgres** 上受支持：事件追加在事务内对着活跃 lease 加围栏，lease 过期按数据库时钟计算，因此每台主机的时钟偏移不会造成脑裂，而且有一个 `worker_id` 列记录持有者。**SQLite** 和**内存版**后端是单主机的 —— 它们没有跨主机围栏，所以把两个 host 进程指向同一个 SQLite 文件是不安全的。

**什么时候撞上：** 你想让多台机器上的 worker 进程排空一个共享存储。

**绕过办法：** 多主机部署请用 Postgres 后端。在 SQLite 上，保持单主机 —— 在那台主机上跑一个多 worker 池是没问题的，而且**同一进程内**任意多个配置各异的 client 可以共享同一个存储三元组：每个 client 起自己的 `queue` 名（`HostConfig.queue`），根任务出生在播种它的 client 的队列上，子任务继承队列，无目标的 worker 轮询绝不跨队列 —— 所以 client 之间不可能驱动彼此的工作。单主机限制针对的是*进程*，不是 client。

见 [ADR：Multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md) 与 [ADR：Worker queue routing](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md)。

### 崩溃恢复不会撤销副作用

**这意味着：** 一次 Step 中途的 worker 崩溃（`kill -KILL`、断电）会在下一次拿 lease 时恢复：被中断的 attempt 被一个持久的 `StepAttemptAbandoned` 标记密封，而当该 attempt 记录下来的一切本来都不需要经过人工审批闸门时，这一步会被重新驱动。当这次 attempt 带有无法证明的副作用时 —— 或者在同一轮里连续密封三次之后 —— 这个 Task 会被**停放**：挂起为一场停下的对话，并带一条 `origin="system"` 的通知，逐条指名被中断的调用以及它是否完成。发生在人工批准过的工具执行期间的崩溃总是停放，并重新挂在同一次审批上。恢复绝不会静默终止一个 Task，也绝不会静默重跑一次有副作用的调用 —— 但它同样无法撤销那次崩溃的 attempt 已经做过的任何事。

**什么时候撞上：** 一次硬杀落在一个已经跑过有副作用工具的 attempt 上。正常的 SIGTERM 关闭不会触发这个，而发生在读取或规划期间的崩溃无需任何人参与就能恢复。

**绕过办法：** 打开那场被停放的对话 —— 通知里列出了被中断的东西。核实那些操作是完全生效、部分生效还是完全没生效，然后打字继续（这一轮会从干净的 attempt 前基线恢复），或者重新批准那次待定的调用。

### 关闭可能放弃一个仍在跑的 Step

**这意味着：** 在 `stop()` 时，`WorkerLoop` 最多等待 `shutdown_grace_s` 让进行中的 Step 完成。如果它没完成，循环会**放弃**这个 Step 并返回 —— 但 Python 无法杀掉被放弃的线程。它可能仍在运行并写入 EventLog。

**什么时候撞上：** 一个 Step 挂住了（比如一次对无响应外部 API 的工具调用），而宽限窗口到期了。

**绕过办法：** **退出进程。** 放弃之后，host 必须调用 `sys.exit()` 或等价物。被放弃的线程随进程一起死掉，它的 lease 过期，`requeue_stale()` 会在下一次启动时回收该 Task。`shutdown_grace_s=None`（或 `<= 0`）会无限等待 —— 那样一个卡住的 Step 就需要外部的 `kill -KILL <pid>`。

### 心跳保活窗口有上限

**这意味着：** 心跳让一个慢 Step 的 lease 保持活着，但不是永远。Dispatcher 把心跳延长次数限制在 `heartbeat_max`（默认 360），因此 `heartbeat_interval × heartbeat_max` 就是一个 Step 能持有 lease 的最长时间。超过上限后 lease 被强制释放，该 Step 的下一次 EventLog 写入会以 `InvalidLease` 失败。

**什么时候撞上：** 单个 Step —— 一次模型轮次加上它的全部工具调用 —— 超过了这个上限窗口。用默认值算这是好几个小时，所以很少见。

**绕过办法：** 把撞上上限当作一个运维故障信号，而不是恢复路径。循环会记录它并继续处理下一个 Task，但被截断的那个 Task 可能需要检查：看它是否还可行，还是应该关掉。

## 可观测性缺口

### 可靠性事件是进程本地的

**这意味着：** worker 会向一个可注入的 sink 发出 `ReliabilityEvent` —— `stale_requeued`、`suspended_without_wake`、`step_failed_retryable`、`heartbeat_invalid_lease`、`shutdown_abandoned`、`timers_fired`、`attempt_abandoned`、`attempt_parked` —— 这个 sink 默认是结构化日志。它们**不是** EventLog 事件，不会被持久化，也活不过一次重启。

**什么时候撞上：** 你要基于 worker 的可靠性信号搭监控或告警。

**绕过办法：** 挂一个自定义的 `reliability_sink`，把它们转发到你的监控系统。每个事件的命名对应 worker 从 Dispatcher 接缝上能证明的东西 —— 比如 `heartbeat_invalid_lease` 是一个现象，它的成因可能是上限、过期，或一次重新入队。

### 没有任何东西通知谁"某个 Task 正在等人"

**这意味着：** human-in-the-loop 完全是带内接好的：引擎挂在一个 `HumanResponseReceived` 唤醒条件上，而 `answer` 这个客户端动词投递响应。没有带外通道 —— 当一个 Task 开始等待时，不会触发任何 webhook、邮件或跨 Task 的收件箱。

**什么时候撞上：** 一个 agent 在没人盯着这个 Task 的时候提了个问题。Task 会持久地等下去，这正是设计的意图，但没有任何东西去告诉别人。

**绕过办法：** 交互式地驱动这个 Task，或者给 EventLog 订阅一个 `Observer`，把 `UserQuestionRequested` 事件转发到你自己的通知渠道，再用 `answer` 投递回复。

## 增长与成本

### 目录里没有的模型会静默关掉 compaction 和定价

**这意味着：** compaction 的各项参数和成本都是从 `providers` 这个 built-in 里的模型目录推导出来的。对于目录未描述的模型，`derive_compaction_config` 返回 `COMPACTION_OFF` —— 上下文 compaction 永远不会介入，因此一场长对话会一直跑到 provider 自己拒绝请求。定价以同样的方式退化：一个没有定价的模型每次往返成本为 `0.0`，因此 `GovernanceState.cost` 停在零，`max_cost_usd` 预算永远不可能触发。这两种退化都不抛异常。

**什么时候撞上：** 你把 `Options.model` 指向一个不在 `CATALOG` 里的网关模型 id、微调模型或自托管模型。

**绕过办法：** 为它注册一行 `ModelSpec` —— `HostConfig(extra_models={...})`，或在进程启动时调用 `noeta.sdk.providers` 的 `register_models`；一行提供 `context_window`、`max_output_tokens` 和价格字段，那就是两处推导所读取的全部内容。

### 内容永远不会被垃圾回收

**这意味着：** ContentStore 是内容寻址且 append-only 的，而且没有发布 GC。`Client.delete_task` 会在整棵子任务树上清除一个 Task 的事件流和 Dispatcher 状态，但刻意不动内容块 —— 它们按哈希在多个 Task 之间共享，所以删掉一个 Task 无法证明某个内容体已不可达。因此存储会随着被记录的工具输出、snapshot 和 compaction 摘要单调增长。

**什么时候撞上：** 一个长期存活、工具输出量很大的部署。

**绕过办法：** 库里没有。按保留期给存储做容量规划，或者对着你的后端写一个离线清扫，遍历剩余各条流的 ref。当树里还有任何 Task 持有活跃 lease 时，`delete_task` 也会以 `reason="running"` 拒绝，所以一次清除永远不会和进行中的一轮抢跑。

## Sandbox 边界

### 库不提供 sandbox 置备器

**这意味着：** `SandboxProvider` 是 SDK 定义并驱动（经由 `SandboxExecEnvManager`）的一个协议，而不是它发布的一个实现。开箱唯一的 provider 把一个 `SandboxExecEnvConfig` 适配成一个**只做 attach** 的 provider：它连接到一个已经在跑的容器，而它的 `release` 是空操作，因为它并不拥有该容器。置备与回收 —— 跑 `docker`、调 K8s API、选择挂载 —— 属于 host。

**什么时候撞上：** 你以为开箱就能每场对话来一个新容器。

**绕过办法：** 在你的 host 里实现 `SandboxProvider`，并把它作为 `HostConfig.sandbox_provider` 传入。`allocate` 返回一个携带寻址信息和一个活跃 `SandboxAuth` 策略的 `SandboxHandle`；`attach` 重新连接到记录在 `TaskHostBound` 上的 `exec_env_ref`，这正是一个被恢复或被回收的会话重新找到自己容器的方式。那次重连能否跨机器工作，取决于你写的那个 provider，而不取决于 SDK。

### sandbox 副作用在 worker 世代之间没有围栏

**这意味着：** 当一个会话跑在 sandbox 容器里时，它的文件和 shell 副作用经 HTTP 发往容器 —— 在那个为 EventLog 写入加围栏的共享 Postgres 事务之外。一个已经被日志围栏挡在外面的 worker（一次 GC 暂停、一次 `SIGSTOP` 后又复活）仍然可以往容器 `POST`。因此 sandbox 副作用是至少一次且无围栏的，与宿主机上跑了一半的 `Bash` 属于同一类：回收方 worker 会重新连到同一个容器并重驱动这一步，但一个慢吞吞的僵尸在这期间可能污染这个容器。因为一个容器属于一棵根 Task 树，僵尸只会污染它自己那场会话。

**什么时候撞上：** 一个持有 sandbox 会话的 worker 卡得足够久，久到它的 lease 过期、另一个 worker 回收了该 Task，然后它醒过来又发出了一次容器调用。

**绕过办法：** 没有自动办法。它受限于覆盖上面那类崩溃 Step 副作用的同一套 Step attempt 重驱动与人工复核。

### sandbox 的 `Bash` 没有远程硬杀

**这意味着：** 在宿主机上，`Bash` 的 `timeout` 映射成一个真正的子进程超时，会杀掉进程。在 sandbox 下没有远程取消动词，所以超时是由那一次调用的 HTTP 读超时在*客户端侧*强制的。你传的 `timeout` 会被遵守 —— 一条跑过头的命令会按你要求的预算被报告给模型为一次超时运行 —— 但调用返回之后，命令**仍在容器里继续跑**。它的副作用可能在工具已经报告超时之后才落地。

**什么时候撞上：** sandbox 里一次 `Bash` 的命令超过了它的 `timeout` —— 一次挂住的构建或测试运行。

**绕过办法：** 把一次超时的 sandbox `Bash` 当作"可能还在跑"；后续一条命令可以观察或清理它的部分效果。给真正耗时长的命令一个显式的更大 `timeout`，好让客户端不会提前掐断这次调用。

### 后台 shell 只在宿主机上可用

**这意味着：** `Bash(run_in_background=true)` 把校验过的 argv 交给 host 的后台运行器，并返回一个 job id，随后由 `BashOutput` 和 `KillShell` 寻址。sandbox 的 `ExecEnv` 会报告它不支持后台执行，于是工具返回一个错误，而不是把命令改到前台跑。

**什么时候撞上：** 一个跑在 sandbox 里的 agent 试图启动一个长期运行的 server 或 watcher。

**绕过办法：** 用一个宽裕的 `timeout` 在前台跑这条命令；或者当后台任务确实必不可少时，让这场会话跑在 sandbox 之外。

### sandbox 浏览器是文本级的，且与容器同生共死

**这意味着：** 一个 sandbox 会话可以通过五个 Noeta 自有的工具驱动容器里的无头浏览器（`browser_navigate`、`browser_click`、`browser_type`、`browser_extract`、`browser_screenshot`）。有三条边界：

- **没有容器就没有浏览器。** 这个工具包只有在会话的后端袋里有一个存活的浏览器后端*并且* agent 激活了 `browser` 时才挂载。否则工具集与非浏览器会话字节一致。
- **感知是文本与元素级的，不是视觉的。** `browser_extract` 返回页面文本，外加一份带编号的可交互元素列表，模型按序号点击和输入。`browser_screenshot` 把一张 PNG 存为工作区产物；它**不会**作为视觉输入喂回模型，因此需要视觉理解的页面无法被完整处理。
- **浏览器与容器同生共死。** 它共享会话容器的生命周期和成本；没有单独的暂停。

**什么时候撞上：** 一个必须读懂只以像素呈现的图表的 Task，或者一个需要在没有容器的情况下浏览网页的 Task。

**绕过办法：** 内容优先用 `browser_extract`，不需要交互的页面用 `WebFetch`；当需要人来看一眼时才用 `browser_screenshot`。

## 封闭的扩展点

### composer 不可替换

**这意味着：** `ContextComposer` 在用户面上是一个封闭的扩展点。稳定前缀的 KV 缓存可复现性是一条硬约束，所以不提供整体替换 composer。开放的钩子只在注册表层面且只增不改：一个 `ContentKindSpec`（一个 semi-stable 常驻内容）或一个组装期的 `reminder`（dynamic-suffix 的尾部）。两者都不碰稳定前缀。

**什么时候撞上：** 你想要一种根本不同的提示布局。

**绕过办法：** 通过那些开放的 Surface 添加常驻内容和 reminder，或者把这个决定挪进一个自定义 `Policy` —— 后者通过 `policy` 这个 Surface *是*可替换的。

## 下一步

- [故障排查](troubleshooting.md) —— 真实故障的症状 → 原因 → 修法
- [架构概览](../architecture/overview.md) —— 完整的系统图景
- [状态与写入者](../architecture/state-and-writers.md) —— 这些边界所依循的那些不变式
- [WorkerLoop 参考](../reference/worker-loop.md) —— 构造函数参数与关闭行为
