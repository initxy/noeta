# Noeta 与其他 agent 框架的对比

如果你正在判断 Noeta 是不是合适的工具，本页就是这场对比的诚实版本——包括那些它是错误选择的情形。

Noeta 是一个面向长程、任务导向 agent 的运行时：它托管、记录、调度并重放 agent 的执行，而不规定 agent 该怎么写。下面关于 Noeta 的每一条陈述都对照本仓库的代码核验过。对其他项目的陈述只限于它们最核心的设计取向——更细的部分请读它们各自的文档。

## Noeta 是什么

- **两个库，进程内运行。** `noeta-runtime` 是内核，不声明任何依赖；`noeta-sdk` 是你唯一需要 import 的东西，承载全部能力实现。没有 CLI，也没有 HTTP 服务器：宿主嵌入 `noeta.sdk` 并自己驱动循环。
- **状态是事件日志上的一次 fold。** 每个任务拥有一条只追加的 `EventLog` 流；任务状态即 `fold(events)`。大体积正文存放在内容寻址的 `ContentStore` 中（单条事件负载上限 4 KB），由 `ContentRef` 引用。
- **等待是一等公民。** 一个任务在一个 `WakeCondition` 上挂起（`SubtaskCompleted` / `HumanResponseReceived` / `TimerFired` / `ExternalEvent`）；`Dispatcher` 把到来的唤醒事件与挂起的任务匹配并重新入队，再由一个 `Worker` 租用这个任务来推进它。
- **压缩是被记录的，不是破坏性的。** 一次压缩步骤发出 `CompactionRequested` 和 `Compacted`；摘要正文进入 `ContentStore`，而 composer 在组装时替换被覆盖的前缀。原始消息仍留在流上，供审计与重放读取。
- **Provider 中立是被强制的。** `LLMProvider` 是内部协议；每个厂商适配器都住在 `providers` 内置插件里。内核够不到任何适配器，因为任何东西都不得静态导入 `noeta.builtins`——`sdk-core-not-builtins` 这条 `import-linter` 契约会在有人这么做时让构建失败。
- **十六个扩展 Surface，一个 loader。** 贡献在一份静态插件 manifest 中声明，横跨三个平面：identity（`tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool`）、wiring（`guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack`）和 host（`mcp_server`、`skills`、`sandbox_provider`）。manifest 是惰性数据——在一个插件的任何代码被 import 之前，它的贡献就已经可列举、可查冲突。
- **子代理就是普通任务。** `spawn_subtask` 和 `spawn_subtasks` 创建拥有自己流的独立事件溯源任务；结果通过一个 `SubtaskCompleted` 唤醒回来，而不是通过一次嵌套调用。
- **治理跑在动作之前。** `Guard` 钩子在 `before_tool_call`、`before_spawn_subtask` 和 `before_finish` 处触发，返回 `allow` / `deny` / `require_approval`；`Observer` 钩子是只读的，它们的失败无法影响任务。

## Noeta 与 Claude Agent SDK

Claude Agent SDK 是一个在 Claude 上构建 agent 的客户端库。它提供一个 agent 循环、内置工具、MCP 支持、子代理、权限模式和钩子，并替你管理对话。

| 关注点 | Claude Agent SDK | Noeta |
| --- | --- | --- |
| **谁拥有底座** | Anthropic 托管模型；这个库在你的进程里跑循环 | 循环、存储和唤醒机制都归你；模型待在一个适配器后面 |
| **持久化什么** | 那场对话，由它替你管理 | 一本事件账本；状态是 `fold(events)`，从不作为主副本存下来 |
| **挂起 / 唤醒** | 恢复一个 session | `WakeCondition` 匹配 + `Dispatcher` + `Lease`，恰好一次投递 |
| **压缩** | 自动摘要 | `CompactionRequested` / `Compacted` 事件；原文仍留在流上 |
| **工具** | 内置工具、`@tool`、MCP | 11 个内置工具、`@tool`（携带 `version` 和 `risk_level`）、基于 stdio 和 HTTP 的 MCP，外加进程内 SDK MCP 服务器 |
| **权限** | `permission_mode`、一个审批回调、钩子 | `permission_mode`（`default` / `acceptEdits` / `bypassPermissions`）、`can_use_tool`，以及在动作之前裁决的 Guard |
| **扩展** | 钩子 | 十六个由 manifest 声明的 Surface，加上单写者规则（observer 不能改动任何东西） |
| **并发** | 进程内一个 client | `Client.start_workers(n)` 提供常驻池；在 Postgres 上支持多主机 |
| **形态** | 一个库，TypeScript 和 Python | 两个 Python wheel：`noeta-runtime`（内核）和 `noeta-sdk`（你 import 的那个） |

两者回答的是不同的问题。这个 SDK 问的是"我怎么给我的代码配一个 agent 循环？"Noeta 问的是"我怎么把一个 agent 的运行变成一本我能重放、能审计、能带走的账本？"

## Noeta 与 LangGraph

LangGraph 把一个 agent 表达为一张由节点和边构成的图，并配一个 checkpointer 来持久化图状态，好让一个线程可以恢复、可以为人类输入中断、可以回退。

| 关注点 | LangGraph | Noeta |
| --- | --- | --- |
| **持久化单元** | 图状态的一个 checkpoint | 一本只追加的事件账本；状态是推导出来的，从不是那份存下来的副本 |
| **历史回答什么** | 某个时刻状态*是什么* | *发生了什么*——每个信封都携带 `actor` / `causation_id` / `trace_id` |
| **控制流** | 你定义的一张图；模型在图内路由 | 没有图。Policy 决定每一步；任务结构从决策中涌现 |
| **调度** | 由调用方重新调起那个线程 | `Dispatcher` + `Lease` + `WorkerLoop` 随库交付，包括过期回收 |
| **压缩** | 应用层的事 | 一个被记录的步骤；摘要在组装时叠加上去 |
| **生态** | 庞大的集成目录，成熟社区 | 小：18 个内置插件，没有市场，社区年轻 |
| **Token 流式传输** | 通过图的事件 API | 通过宿主提供的 `HostConfig.delta_sink`；delta 是瞬时的，账本仍是唯一的持久记录 |

想要一张图和一份集成目录时，选 LangGraph。当问题是可审计性和底座所有权时——哪个工具以谁的授权跑过、什么被压缩掉了、什么唤醒了一个沉睡的任务——并且你希望调度机制在库里而不是在一个托管产品里时，选 Noeta。

## Noeta 与 Temporal

Temporal 是一个持久执行平台：你用代码编写 workflow 和 activity，由服务持久地调度、重试和计时它们。

Noeta 不是一个工作流引擎。控制流由 LLM 驱动，因此一个任务的形状是从模型的决策中涌现的，而不是来自一份事先写好的定义。当你知道工作的形状时，Temporal 合适；当模型边走边发现它时，Noeta 合适。Noeta 把 `Workflow` 挡在自己的词汇之外——固定流程被表达为一个确定性 Policy 加上 `spawn_subtask`。

## Noeta 与 Google Cloud Agent SDK

Cloud Agent SDK 在 Google Cloud 上构建 agent：Gemini 模型、接到 GCP 服务（BigQuery、Cloud Storage 等）的工具，以及一个驱动 agent 循环的 `Runner`。它是一个客户端库——agent 跑在你的进程里，但底座（模型、工具集成）是 Google 的。

| 关注点 | Cloud Agent SDK | Noeta |
| --- | --- | --- |
| **部署** | 客户端库，单进程 | 多 worker 池；在 Postgres 上多主机，写入受租约 fencing 保护 |
| **模型** | Gemini 优先 | `LLMProvider` 后面的任何 provider |
| **持久化** | 对话状态，由它管理 | 事件账本；`state = fold(events)` |
| **挂起 / 唤醒** | Session 恢复 | `WakeCondition` + `Dispatcher` + `Lease`，恰好一次 |
| **工具生态** | GCP 服务集成 | 11 个内置工具 + 你的插件；MCP |
| **扩展** | 工具 + 钩子 | 16 个由 manifest 声明的 Surface |
| **审计 / 重放** | 有限 | 完整事件日志，fold 可复现 |

当你全面押注 Google Cloud、并且想开箱即用地拿到 GCP 服务工具时，选 Cloud Agent SDK。当你需要一本持久的、provider 中立的、可审计可重放的账本，并且想把它作为一个多 worker 或多主机服务来跑、而不是一个单进程客户端时，选 Noeta。

## Noeta 与 Pi Agent

Pi Agent 是一个 computer-use 框架：它把鼠标、键盘和屏幕截取交给一个 LLM 控制，好让 agent 驱动一个桌面 GUI。它是物理计算机之上的一个控制层，不是一个 agent 运行时。

| 关注点 | Pi Agent | Noeta |
| --- | --- | --- |
| **部署** | 桌面进程 | 多 worker 池；在 Postgres 上多主机 |
| **它做什么** | 让一个 LLM 点击、输入和读屏 | 托管、记录并调度 agent 的执行 |
| **持久化** | 瞬时——没有持久执行模型 | 事件溯源的账本，崩溃安全且可重放 |
| **挂起 / 唤醒** | 不适用 | 一等公民：人、定时器、子任务、外部 |
| **模型** | 任意 LLM（它是一个控制层） | `LLMProvider` 后面的任何 provider |
| **工具** | 鼠标 / 键盘 / 截屏原语 | fs、web、memory、browser、MCP、你的插件 |
| **审计** | 无 | 完整事件日志 |

Pi Agent 和 Noeta 解决的是不同的层。Pi Agent 回答"agent 怎么与一个 GUI 交互？"Noeta 回答"agent 的运行怎么变成一本持久、可审计的账本？"两者是互补的：一个 Noeta 任务完全可以通过 `browser` 内置项或一个自定义工具插件去调用 Pi Agent 式的 computer-use 工具。

## Noeta 在什么时候是错误的选择

基础设施是你来运维的。多主机部署需要 Postgres 后端；SQLite 和内存后端是单主机的。内置工具集很小，也没有插件市场。如果你的要求是"它能对着某个厂商的 API 工作，而且没有任何运维面"，那么一个托管的客户端库摩擦更小。

## 下一步

- [快速上手](../tutorials/quickstart.md) —— 五分钟试一下
- [事件溯源](../concepts/event-sourcing.md) —— 状态为什么是 `fold(log)`
- [已知限制](../operations/limitations.md) —— 各项边界的细节
- [架构概览](../architecture/overview.md) —— 完整图景
