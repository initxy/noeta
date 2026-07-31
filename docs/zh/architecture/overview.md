# 架构概览

一屏之内自顶向下地走读 Noeta：两个包如何堆叠、一轮里跑了什么、扩展点在哪。每一节末尾都链到深入讲解那一块的页面。"X 是什么"这类问题请从[核心概念](../concepts/index.md)开始；要看签名，去 [SDK 参考](../reference/sdk.md)。

| 深入 | 回答什么 |
| --- | --- |
| [包与导入规则](packages.md) | 为什么是两个 wheel、一个命名空间，以及导入规则禁止了什么 |
| [状态与写入者](state-and-writers.md) | "state = fold(log)" 是如何被强制的，而不只是被承诺 |
| [扩展平面](extension-planes.md) | 十六个 Surface、加载器，以及什么是锁死的 |

## 这套栈

Noeta 是两个共享同一个 PEP 420 `noeta.` 命名空间的库。**`noeta-runtime`** 是纯内核 —— Engine、fold、snapshot，Worker / Dispatcher / lease 机制，以及上下文 composer —— 不携带任何能力实现，也不含 HTTP 客户端。**`noeta-sdk`** 是你导入的那个薄客户端，外加 `noeta.builtins`，每个官方能力都以插件形式住在那里。

那条承重规则是：**没有任何东西静态导入 `noeta.builtins`**。唯一的入口是插件加载器的动态 `ref` 解析，由 CI 里的 import-linter 强制执行。因为每个厂商适配器都住在那一层里，provider 中立是结构性的 —— 内核*够不到*任何一个。
→ [包与导入规则](packages.md)

## 事实基础

一个 Task 的事实基础是它的 append-only 事件日志。状态通过 fold 这份日志计算出来，从不作为一等副本存储，这正是让崩溃的 worker 可恢复、让半年前的 Task 可恢复的原因。

有两个机制让这个承诺站得住脚：状态被切成四个带类型的切片，每片恰好一个写入者；而每一次 EventLog 追加都被一个活跃 lease 加了围栏，因此同一时刻只有一个 worker 能写某个 Task 的流。
→ [状态与写入者](state-and-writers.md) ·
[事件溯源](../concepts/event-sourcing.md)

## 一步，以及步与步之间的等待

**Engine** 把一个 Task 推进一步 —— compose → decide → dispatch —— 并在遇到 `tool_calls` 决策时在内部循环，因此一次调用覆盖了完整的一轮。它对 worker、Dispatcher 或 HTTP 一无所知。

**Dispatcher** 拥有调度：入队、授予 lease、投递唤醒、回收过期。**Worker** 租下一个 Task、fold 它的日志、驱动一步，然后释放。排空循环以库原语 `noeta.runtime.worker.WorkerLoop` 的形式发布 —— 没有任何东西替你启动它。

步与步之间，Task 挂在一个唤醒条件上 —— 一个人的回答、一个定时器、一个子任务、一个外部事件 —— 睡着时不花任何成本。这次匹配被持久保存，在拿 lease 时投递，并由一次 `TaskWoken` 写入落定；至少一次投递加上幂等消费给出恰好一次恢复，并被围栏限定在单个 worker 上。
→ [引擎与执行](../concepts/engine-execution.md) ·
[唤醒与恢复](../concepts/wake-resume.md) ·
[WorkerLoop 参考](../reference/worker-loop.md)

## 上下文组装

每一步，`ThreeSegmentComposer` 都会从 fold 出来的状态按易变程度组装出模型的 View，分三段 —— `stable_prefix`、`semi_stable`、`dynamic_suffix` —— 让前缀保持字节稳定，从而让 provider 的 KV 缓存活下来。compaction 是一个被记录的事件，而不是原地编辑，因此原始内容仍留在流上。
→ [Composer 与缓存](../concepts/composer-and-cache.md)

## 你可以扩展什么

一切开放的东西要么是一个 `Options` 字段，要么是一条插件贡献，分布在三个平面上：**身份**（工具、agent、提示词片段、内容 kind、Policy、控制工具 —— 这些会进入持久的 agent 身份）、**接线**（Guard、Observer、provider、reminder、session pack —— 进程级或 host 级作用域），以及 **host**（MCP 服务器、skills、sandbox provider）。

Noeta 自己的能力走的是完全相同的路径，所以这套扩展面被每个默认 agent 在每次运行中反复检验。Engine 主循环、lease 协议和 composer 保持锁死。
→ [扩展平面](extension-planes.md) ·
[编写插件](../how-to/write-a-plugin.md)

## 部署形态

默认形态是单主机：一个 SQLite 文件加上一个进程内的常驻 worker 池。多主机只是换一个存储适配器 —— 指向 Postgres，多个 host 进程共享一个数据库，并带事务内的 lease 围栏。两种情况下 Engine 都不变，因为任何能读到存储的进程都能通过 fold 重建任何 Task。
→ [部署 Worker](../how-to/deploy-worker.md) ·
[已知限制](../operations/limitations.md)

## 接下来去哪

- [核心概念](../concepts/index.md) —— 词汇表，一个想法一页
- [SDK 参考](../reference/sdk.md) · [插件参考](../reference/plugins.md)
- [已知限制](../operations/limitations.md) —— 设计在哪里停下
- [`docs/adr/`](https://github.com/initxy/noeta/tree/main/docs/adr) —— 每个决策背后的理由
