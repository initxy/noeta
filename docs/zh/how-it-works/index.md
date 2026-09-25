# 工作原理

想知道 Noeta 底下是怎么运转的，读这一页就够了。整套设计靠三个想法撑着，其余的——崩溃恢复、审计、重放、挂起和恢复、随意换模型——都是从这三点推出来的。

<NtArchitecture lang="zh" />

## 1. 状态由事件日志算出来

每个 Task 有一条只能追加的事件流：目标、每次给模型看的上下文、每次模型回复、每次工具调用和结果、每次挂起和唤醒。没有任务表。谁要当前状态，就从头（或从最近的快照）把这条流 fold 一遍得到。

- **崩溃恢复不用额外代码。** worker 死了也不会留下写了一半的状态，下一个 worker fold 日志接着干。
- **重放完全一致。** 同一份日志在任何机器上 fold 出来的状态逐字节相同。
- **天然可审计。** 日志本身就是历史，没有任何记录会被原地改写。

→ [事件日志](event-log.md)

## 2. Task 是唯一的工作单元，等待是常态

跑了几周的对话、每晚的定时作业、委派出去的子代理，都是 Task，各有自己的日志。没有 session，也没有 workflow 实例。Task 只有四种状态：`pending`、`running`、`suspended`、`terminal`。所有等待——等人回复、等定时器、等子任务、等外部事件——都是同一个 `suspended` 状态，外加一个带类型的唤醒条件。

- 挂起的 Task 不占线程、不占连接、不占内存，等几个月也没关系。
- 匹配上的唤醒会持久保存，只让 Task 恢复一次，中途 worker 死掉也一样。
- worker 运行 Task 时持有租约（lease），每次写日志都要出示租约，所以一个 Task 永远不会有两个写入方。

→ [任务与唤醒](tasks-and-waking.md)

## 3. 内核不带任何能力，一切都是插件

文件工具、网页工具、记忆、MCP、沙箱、存储后端、guard、每一个模型适配器，都是 `noeta.builtins` 下的内置插件。内核只能通过插件加载器按 `ref` 字符串动态导入它们；只要有代码静态 import 它们，import linter 就让构建失败。你写的插件和 Noeta 自己的插件走完全相同的加载、校验和合并流程。

内核 import 不到任何厂商适配器，自然也不会偷偷依赖某家厂商的格式。模型怎么接入见[接入模型](../guides/models.md)。

→ [插件系统](plugin-system.md)

## 两个包

| 包 | 是什么 | 依赖 |
| --- | --- | --- |
| `noeta-runtime` | 内核：Engine、fold、快照、Worker、Dispatcher、租约、上下文组装。不含任何能力代码，也没有 HTTP 客户端。 | 只用标准库 |
| `noeta-sdk` | 你安装和 import 的包（`noeta.sdk`）：`query`、`Client`、`Options`、`@tool`、预设 agent，以及 `noeta.builtins`。 | `noeta-runtime`、`httpx`、`psycopg` |

两个包共用 `noeta.` 这个命名空间。你只需安装 `noeta-sdk`，`noeta-runtime` 会跟着装上。

## 一轮对话从头到尾

1. 你的代码把目标交给 `query()` 或 `Client`。
2. 某个 worker 拿到这个 Task 的租约，把日志 fold 成状态。
3. Engine 循环执行「组装 → 决策 → 执行」：拼出模型要看的内容，由 policy 选下一步动作，跑工具，每个结果都记成事件。动作执行前，guard 可以拦下它。
4. Task 完成或需要等待时循环停下，worker 释放租约。等待中的 Task 在唤醒到来之前没有任何开销。

部署形态不影响 Engine：单进程加 SQLite、服务里的 worker 池、多台机器共用 Postgres，fold 的都是同样的日志。见[部署](../guides/deploy.md)。

## 接着读

- [事件日志](event-log.md)：状态怎么从日志算出来，快照，崩溃恢复。
- [任务与唤醒](tasks-and-waking.md)：四种状态、子任务、只恢复一次的唤醒。
- [Engine](engine.md)：一步是怎么走的，决策有哪些，guard 和 observer 的区别。
- [上下文与缓存](context.md)：每次的 prompt 怎么拼，才能让 provider 的缓存一直命中。
- [插件系统](plugin-system.md)：扩展点、加载器，以及哪些部分不开放。
