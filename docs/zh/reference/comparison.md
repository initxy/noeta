# 对比：Noeta 与其他 agent 框架

Noeta 是一个面向长程、任务导向 agent 的运行时：它托管、记录、调度并重放 agent 的执行，而不规定 agent 该怎么写。

下面关于 Noeta 的每一条陈述都对照本仓库的代码核验过。对其他项目的陈述只限于它们最核心的设计取向——更细的部分请读它们各自的文档。

## Noeta 是什么

- **两个库，进程内运行。** `noeta-runtime` 是内核，不声明任何依赖；`noeta-sdk` 是你唯一需要 import 的东西，承载全部能力实现。没有 CLI，也没有 HTTP server：host 嵌入 `noeta.sdk` 并自己驱动循环。
- **状态是事件日志上的一次 fold。** 每个任务拥有一条只追加的 `EventLog` 流；任务状态即 `fold(events)`。大体量的主体存放在内容寻址的 `ContentStore` 中（单条事件载荷上限 4 KB），由 `ContentRef` 引用。
- **等待是一等公民。** 任务在某个 `WakeCondition`（`SubtaskCompleted` / `HumanResponseReceived` / `TimerFired` / `ExternalEvent`）上挂起；`Dispatcher` 将到来的唤醒事件与被挂起的任务匹配并重新入队，`Worker` 租用该任务将其推进。
- **压缩是被记录的，而非破坏性的。** 一次压缩步骤发出 `CompactionRequested` 加 `Compacted`；摘要主体进入 `ContentStore`，composer 在组装时替换被覆盖的前缀。原始消息仍留在流上，审计与重放都能读到。
- **Provider 中立是强制的。** `LLMProvider` 是内部协议；每个厂商适配器都住在 `providers` 内置插件里。内核无法触及适配器，因为任何代码都不得静态 import `noeta.builtins`——一旦有，`sdk-core-not-builtins` 这条 `import-linter` 契约就会让构建失败。
- **十六个扩展 Surface，一个 loader。** 贡献在静态插件 manifest 中跨三个平面声明：identity（`tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool`）、wiring（`guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack`）与 host（`mcp_server`、`skills`、`sandbox_provider`）。manifest 是惰性数据——在导入插件的任何代码之前，它的贡献就可列举、可做冲突检查。
- **子代理就是普通任务。** `spawn_subtask` 和 `spawn_subtasks` 创建各自拥有独立流的、事件溯源的独立任务；结果通过 `SubtaskCompleted` 唤醒回流，而不是嵌套调用返回。
- **治理在行动之前运行。** `Guard` 钩子在 `before_tool_call`、`before_spawn_subtask` 和 `before_finish` 三处触发，返回 `allow` / `deny` / `require_approval`；`Observer` 钩子只读，它们的失败无法影响任务。

## Noeta 与 Claude Agent SDK

Claude Agent SDK 是一个在 Claude 上构建 agent 的客户端库。它自带一个 agent 循环、内置工具、MCP 支持、子代理、权限模式和钩子，并替你管理对话。

| 关注点 | Claude Agent SDK | Noeta |
| --- | --- | --- |
| **谁拥有底层** | Anthropic 托管模型；库在你的进程里跑循环 | 循环、存储、唤醒机制都归你；模型在一个适配器之后 |
| **持久化什么** | 对话，替你管理 | 一份事件账本；状态是 `fold(events)`，从不作为主副本存储 |
| **挂起 / 唤醒** | 恢复一个 session | `WakeCondition` 匹配 + `Dispatcher` + `Lease`，恰好一次交付 |
| **压缩** | 自动摘要 | `CompactionRequested` / `Compacted` 事件；原始内容留在流上 |
| **工具** | 内置工具、`@tool`、MCP | 11 个内置工具、`@tool`（携带 `version` 和 `risk_level`）、stdio 与 HTTP 之上的 MCP，外加进程内 SDK MCP server |
| **权限** | `permission_mode`、一个审批回调、钩子 | `permission_mode`（`default` / `acceptEdits` / `bypassPermissions`）、`can_use_tool`，以及在行动之前裁决的 Guard |
| **扩展** | 钩子 | 十六个 manifest 声明的 Surface，外加单写者规则（observer 不能修改） |
| **并发** | 进程内一个 client | `Client.start_workers(n)` 提供常驻池；在 Postgres 上支持多 host |
| **形态** | 一个库，TypeScript 和 Python | 两个 Python wheel：`noeta-runtime`（内核）和 `noeta-sdk`（你 import 的那个） |

两者回答的是不同的问题。SDK 问的是"我怎么给我的代码配一个 agent 循环？"Noeta 问的是"我怎么把一个 agent 的运行变成一份我能重放、审计、带到别处的账本？"

## Noeta 与 LangGraph

LangGraph 把 agent 表达为节点与边构成的图，配一个 checkpointer 持久化图状态，好让一个线程能被恢复、为等待人工输入而中断，以及回退。

| 关注点 | LangGraph | Noeta |
| --- | --- | --- |
| **持久化单元** | 图状态的一个 checkpoint | 只追加的事件账本；状态是派生出来的，从不作为被存储的副本 |
| **历史回答什么** | 某一时刻状态*是什么* | *发生了什么*——每个 envelope 携带 `actor` / `causation_id` / `trace_id` |
| **控制流** | 你定义的图；模型在其中路由 | 没有图。Policy 决定每一步；任务结构从决策中涌现 |
| **调度** | 调用方重新调用该线程 | `Dispatcher` + `Lease` + `WorkerLoop` 随库交付，含过期回收 |
| **压缩** | 应用自己的事 | 一个被记录的步骤；摘要在组装时叠加 |
| **生态** | 庞大的集成目录、成熟的社区 | 小：18 个内置插件，没有市场，社区年轻 |
| **Token 流式** | 通过图的事件 API | 通过 host 提供的 `HostConfig.delta_sink`；delta 是临时的，账本始终是唯一的持久化记录 |

想要一张图和一个集成目录时，选 LangGraph。当问题是可审计性和底层所有权——哪个工具在谁的授权下运行了、什么被压缩掉了、什么唤醒了一个沉睡的任务——并且你希望调度机制在库里而不是在托管产品里时，选 Noeta。

## Noeta 与 Temporal

Temporal 是一个持久化执行平台：你用代码写 workflow 和 activity，服务持久化地调度、重试并为它们计时。

Noeta 不是 workflow 引擎。是 LLM 驱动控制流，所以一个任务的形态从模型的决策中涌现，而非来自一份提前写好的定义。当你知道工作的形态时，Temporal 合适；当模型在过程中发现它时，Noeta 合适。Noeta 把 `Workflow` 排除在词汇之外——固定过程用一个确定性 Policy 加 `spawn_subtask` 来表达。

## 什么时候 Noeta 是错误的选择

基础设施由你来运维。多 host 部署需要 Postgres 后端；SQLite 与内存后端是单 host 的。内置工具集很小，也没有插件市场。如果需求是"对着某个厂商的 API 就能用，没有任何运维面"，那么托管的客户端库是摩擦更低的选择。

## 另见

- [事件溯源](../concepts/event-sourcing.md)——为什么状态 = fold(log)
- [唤醒与恢复](../concepts/wake-resume.md)——交付保证
- [已知限制](../operations/limitations.md)——边界的细节
- [架构概览](../architecture/overview.md)——全景图
