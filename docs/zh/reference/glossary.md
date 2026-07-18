# 术语表

Noeta 的规范词汇。每个术语在整个代码库和文档中都有单一、稳定的含义。权威来源是仓库根目录的[`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md)。

## 核心抽象

### Task

代理的一个执行实例；它可以生成子任务，可以挂起和恢复。系统中唯一的一等公民。
_避免使用：_ Run、Job、Execution、Workflow Instance。

另见：[核心概念](/concepts/task-model)、[ADR：任务作为唯一原语](https://github.com/initxy/noeta/blob/main/docs/adr/task-as-only-primitive.md)

### Subtask

通过 `spawn_subtask` 从父任务生成的任务。结构上与父任务相同，仅通过 `parent_task_id` 关联。
_避免使用：_ Child Run、Sub-agent。

另见：[核心概念](/concepts/wake-resume)、[ADR：子任务扇出与持久唤醒](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)

### Agent

一个命名的、可生成的配置（policy + 工具 + 上下文规范 + 预算）。**不是运行时实体**——只是任务的"类"。每个 Agent 携带一个 `description`，用于渲染子代理分派控制工具 schema。
_避免使用：_ Bot、Assistant、AI。

另见：[预设代理](presets.md)、[ADR：工具与代理目录](https://github.com/initxy/noeta/blob/main/docs/adr/tool-and-agent-catalog.md)

### Options

声明式代理配置（公开接口 `noeta.sdk.Options`）。由 `compile_options` 编译为 `AgentSpec`。**表达官方代理集和自定义代理的唯一方式。**

另见：[API 参考](/reference/sdk)、[配置](configuration.md)

### Step

任务在一次 Engine 主循环中前进的片段：`compose_view → decide → dispatch`。
_避免使用：_ Iteration、Turn、Cycle。

### Decision

`Policy.decide` 的返回值，Engine 分派的输入。一组中立机制变体：`tool_calls`、`spawn_subtask`、`yield_for_human`、`wait_timer`、`wait_external`、`finish`、`fail`、`spawn_subtasks`、`state_patch`。
_避免使用：_ Action、Command、Intent。

另见：[核心概念](/concepts/engine-execution)

### Policy

"在给定当前 View 的情况下决定下一步"的函数。可以是纯 LLM（ReActPolicy）、纯 FSM 或混合体。
_避免使用：_ Pattern、Strategy、Brain。

另见：[核心概念](/concepts/engine-execution)、[ADR：Engine-Policy-数据流](https://github.com/initxy/noeta/blob/main/docs/adr/engine-policy-dataflow.md)

### Tool

代理可以调用的外部动作。结构化契约三元组 `name` / `input_schema` / `description` 是手写的、面向 LLM 的。还携带 `risk_level`。通过 `Options` 提供的**开放**扩展接口。
_避免使用：_ Function、Action、Skill。

另见：[工具参考](tools.md)、[ADR：工具描述规范化](https://github.com/initxy/noeta/blob/main/docs/adr/tool-description-canonical.md)

### Provider

外部服务（LLM / 存储 / 向量存储）的 Noeta 形态 adapter。`LLMProvider` 通过 `Options.provider` 开放，并通过 `noeta.sdk` 重新导出。存储后端通过**主机配置**配置，而非 Options。**不是上下文内容来源。**
_避免使用：_ Vendor、Backend、Connector。

另见：[配置](configuration.md#provider-adapters)、[ADR：Provider adapters and multimodal](https://github.com/initxy/noeta/blob/main/docs/adr/provider-adapters-and-multimodal.md)、[ADR：提供者中立](https://github.com/initxy/noeta/blob/main/docs/adr/provider-neutral.md)

### Skill

位于 `.noeta/skills/<name>/SKILL.md` 的本地静态 LLM 工作流模板，可选附带资源文件。三层合并（内置 < 全局 `~/.noeta/skills` < 工作区）。两阶段按需加载：菜单渲染到 `skill` 控制工具 schema；选中后主体渲染到半稳定上下文。**与 Tool 不是一回事。**
_避免使用：_ Plugin、Module、Macro。

另见：[ADR：模型驱动的技能调用](https://github.com/initxy/noeta/blob/main/docs/adr/model-driven-skill-invocation.md)、[ADR：技能资源按需加载](https://github.com/initxy/noeta/blob/main/docs/adr/skill-resource-on-demand.md)

## 状态与事件

### EventLog

每任务的只追加 `EventEnvelope` 记录流。**因果关系和决策的真相来源。**
_避免使用：_ Journal、Log、Audit Trail。

另见：[核心概念](/concepts/event-sourcing)、[ADR：事件溯源真相](https://github.com/initxy/noeta/blob/main/docs/adr/event-sourced-truth.md)

### Event / EventEnvelope

EventLog 中的一条记录。Envelope 持有 `seq / type / actor / trace_id / causation_id`；载荷是一个类型化的 dataclass。
_避免使用：_ Message、Record。

### ContentStore

内容寻址、不可变的大对象存储。**大对象的真相来源。** 大于 4 KB 事件载荷上限的主体存放在此处；envelope 仅携带 `ContentRef`。
_避免使用：_ BLOB Store、Asset Store、Object Store。

另见：[核心概念](/concepts/event-sourcing)、[ADR：存储协议 L0](https://github.com/initxy/noeta/blob/main/docs/adr/storage-protocols-l0.md)

### ContentRef

指向 ContentStore 的引用：`hash + size + media_type`。
_避免使用：_ URL、Path、Pointer。

### Artifact

由 Tool 或 Provider 产生的大对象，通过 ContentRef 引用。
_避免使用：_ File、Attachment、Blob。

### Snapshot

EventLog 中的一个特殊事件，其主体存入 ContentStore。在每次挂起之前写入；是 fold 的加速点。
_避免使用：_ Checkpoint、State Dump。

### Task State（四个切片）

四个类型化切片，每个切片恰好有一个写者：

- **RuntimeState** —— messages / usage（写者：Engine）
- **TaskState** —— goal / phase / todos / decisions / active_content（写者：Policy 的 `state_patch`）
- **ContextState** —— current plan ref（写者：Engine fold）
- **GovernanceState** —— cost / denied（写者：Engine）

## 执行模型

### Engine

通过一步推进单个 Task。≤ 500 行。对 worker / dispatcher / workflow 一无所知。**锁定**：不是扩展点。
_避免使用：_ Runtime、Executor。

另见：[核心概念](/concepts/engine-execution)、[ADR：Engine-Policy-数据流](https://github.com/initxy/noeta/blob/main/docs/adr/engine-policy-dataflow.md)

### Worker

从 Dispatcher 租用 Task 并调用 Engine 推进它的进程。**一个租约运行到下一次挂起或终止状态，然后释放。**
_避免使用：_ Runner、Daemon。

另见：[核心概念](/concepts/engine-execution)、[ADR：Worker 租约模型](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md)

### Lease

Worker 对 Task 的短期独占持有，带有 `lease_id / expires_at`。
_避免使用：_ Lock、Claim。

另见：[ADR：Worker 租约模型](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md)、[ADR：单写者不变量](https://github.com/initxy/noeta/blob/main/docs/adr/single-writer-invariant.md)

### Dispatcher

管理 Task 入队、Lease 授予、唤醒事件交付和过期回收。
_避免使用：_ Scheduler、Queue Manager。

另见：[核心概念](/concepts/wake-resume)、[ADR：Worker 租约模型](https://github.com/initxy/noeta/blob/main/docs/adr/worker-lease-model.md)

### Suspended

Task 的 4 种状态之一，等待唤醒事件。等待子任务 / 批准 / 计时器 / 外部事件的**统一表达**。
_避免使用：_ Yielded、Paused、Blocked、Waiting。

### WakeCondition / WakeEvent

描述 Task 正在等待什么。`SubtaskCompleted` / `HumanResponseReceived` / `TimerFired` / `ExternalEvent`。

另见：[核心概念](/concepts/wake-resume)、[ADR：子任务扇出与持久唤醒](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md)

## 上下文

### View

ContextComposer 为 Policy 组装的 LLM 输入。**不等于 Task**——它是一个投影。
_避免使用：_ Prompt（View 是 Prompt 的结构化形式）、Frame。

### ContextComposer

将 Task 组装为 View。主路径不调用 LLM。具体的 `ThreeSegmentComposer` 在用户界面上是一个**封闭的**扩展点（stable-prefix KV-cache 可重现性是硬约束）。唯一的开放钩子是注册一个 `ContentKindSpec`。
_避免使用：_ PromptBuilder、ContextAssembler。

另见：[ADR：统一上下文供给](https://github.com/initxy/noeta/blob/main/docs/adr/unified-context-supply.md)、[ADR：上下文压缩](https://github.com/initxy/noeta/blob/main/docs/adr/context-compaction.md)

### ContextPlan

给定 LLM 调用的 View 元数据（选择了哪些块、压缩了什么、丢弃了什么）。用于审计和调试。
_避免使用：_ Prompt Trace。

### Stable Prefix / Semi-stable / Dynamic Suffix

View 三段组装中的固定段名称。Stable Prefix 的缓存友好性是硬约束。

### Content Channel

常驻内容（技能、记忆索引）进入上下文的通用机制。两部分：**事件记录**（`ContextContentRecorded`）+ **组装渲染**（`ContentChannelRegistry` 将每种类型渲染到半稳定段）。注册 `ContentKindSpec` 是开放扩展钩子。
_避免使用：_ Provider、ContentSource、Middleware。

另见：[ADR：模型驱动的技能调用](https://github.com/initxy/noeta/blob/main/docs/adr/model-driven-skill-invocation.md)

### origin

`Message` 上的可选作者标记，取值 `human / system / memory`，默认为 `None` = 角色的自然作者。**单写者守卫**：只有 Engine 的记录路径可以写入它。
_避免使用：_ Author、Sender、Role。

另见：[ADR：事件来源标记](https://github.com/initxy/noeta/blob/main/docs/adr/event-origin-marker.md)

### Memory

跨任务长期记忆（v2）：**改** = `memory_write`（可选 frontmatter `description` / `type`）与 `memory_archive`（移入 `archive/` 归档，从不删除）工具，**读** = `memory_read`（按名取全文）与 `memory_search`（子串搜索、返回摘录）工具，**常驻索引** = 内容通道租户（`kind="memory"`，policy `evolving`），**自动召回** = 主机在用户消息接缝处检索（先按名字 token，再按摘要 token），**政策** = 追加在启用 memory 的 preset prompt 上的 `MEMORY_POLICY_PROMPT` 片段。由 `Capabilities.memory` 控制。后台**整理（consolidation）**由隐藏的 `__consolidation__` agent 在常驻 worker 池上运行（会话停止触发、防抖），对记忆做合并 / 归档 / 补写；其开关是主机配置而非 agent 身份——见 [ADR：Memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md)。
_避免使用：_ 用 "Memory" 指代 TaskState（那是任务内状态；这是跨任务的）。

## 治理

### Principal

Task 的发起者或责任方；持有 identity / capabilities / allowed_side_effects / delegation chain。
_避免使用：_ User（User 是一种 Principal）、Actor。

### Contract

Task 的输入、预期输出 schema、拒绝条件和副作用声明。冻结到 `TaskCreated` 事件中。
_避免使用：_ Spec、Schema。

### Budget

Task 的资源上限（iterations / cost_usd / wall_seconds / tool_calls）。
_避免使用：_ Quota、Limit。

### Guard

在三个点运行的同步钩子——`before_tool_call` / `before_spawn_subtask` / `before_finish`——返回 `allow / deny / require_approval`。
_避免使用：_ Middleware、Interceptor、Filter。

另见：[核心概念](/concepts/guard-observer)、[ADR：Guard-Observer 钩子](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md)

### Observer

订阅 EventLog 的异步钩子；其失败不影响 Task。
_避免使用：_ Listener、Subscriber。

另见：[核心概念](/concepts/guard-observer)、[ADR：Guard-Observer 钩子](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md)

### Mutator

**在 Noeta v2 中已弃用。** 钩子不得修改 ctx / payload。要修改，请改为更改 Policy 或 Composer。

## 操作

### Inspect

读取 EventLog + ContentStore 并向人类呈现历史。无外部 IO。
_避免使用：_ View Log、Dump。

### Resume

从挂起状态继续实际执行。一个操作紧急停止杠杆；正常路径由唤醒事件触发。
_避免使用：_ Restart、Continue。

另见：[故障模式](/operations/troubleshooting)

## 应用层（noeta-agent 平台）

由产品拥有的词汇（[ADR：server-platform 产品](https://github.com/initxy/noeta/blob/main/docs/adr/server-platform-product.md)）。这些术语没有一个存在于应用层之下：Engine 只认识 Task。

### Session

应用层的对话单元——UI 所列出、恢复和删除的东西。归属于一个用户，限定在一个 Space 内；聚合**一个或多个 Engine Task**（workflow session 的每个节点各拥有一个根任务），并拥有一个工作区目录和一个沙箱容器。仅是应用层索引：持久化在应用数据库中；EventLog 仍是唯一的真相来源。
_避免使用：_ Conversation、Thread；在应用层之下使用 Session。

### Space

协作与作用域的单元。用户属于 space；space 限定技能、知识源、代理记忆、MCP 连接器、agent-config 和模板的作用范围。每个用户都有一个个人 space；团队 space 的成员由 owner 管理。会话可见性 = space 成员身份。
_避免使用：_ Team、Organization、Workspace（Workspace 是会话的文件根目录）。

### UI event

产品线上词汇的一帧（`user_message`、`assistant_text`、`thinking`、`tool_call` / `tool_result`、`skill_activated`、`todo_update`、`subtask_started` / `subtask_finished`、`question`、`compaction`、轮次标记等），由**翻译器（translator）**产生——一个作用于 `EventEnvelope` 的确定性、无状态的纯函数。重放就是经由 `since_seq` 从 EventLog 重新推导；token 增量是短暂的，从不重放。原始 envelope 只出现在管理端 trace 表面。
_避免使用：_ 把原始 `EventEnvelope` 称为 UI event；"projection"（暗示存在一份存储的副本）。

### Skill registry

平台的、由数据库支撑的技能表面：**builtin skills**（管理员管理、平台级）和 **space skills**（各 space 由 owner 上传），二者都以只读方式挂载进会话沙箱，并渲染进模型的技能菜单。它是库级 Skill 格式之上的管理层（`SKILL.md` 保持不变）。
_避免使用：_ Skill market、Plugin store。

### Knowledge source

space 级的同步内容源，带可插拔的同步适配器；开源核心自带 `git_repo` 和 `local_dir`。物化在共享数据目录之下，以只读方式挂载进沙箱，通过 agent-config 选入组装。
_避免使用：_ RAG index（没有向量库）、Dataset。

### MCP connector

每个 space 一份的 MCP 服务器配置：alias + 传输方式（`http` | `stdio`）+ 凭据 + 启用的工具子集，存储在应用数据库中，每次读取都会擦除凭据。每轮解析进代理主机；工具以 `mcp__<alias>__<tool>` 出现。取代已退役的全局 `~/.noeta/mcp_servers.json` 注册表。
_避免使用：_ 全局 MCP 注册表（已退役）、Plugin。

### Agent-config

space 的代理配置：persona 提示词（组装时写入会话工作区的 `AGENT.md`）、默认模型/推理力度（reasoning effort）、知识源选择、记忆开关。由 owner 通过 `GET/PUT /api/v1/spaces/{id}/agent-config` 管理。
_避免使用：_ Options（SDK 级的代理配置）、Settings（服务器配置）。

### Feedback loop

space 成员的逐消息评分，供给一个由 owner 触发的分析代理，其建议由 owner 把关：采纳进 space 记忆、应用技能补丁（先备份），或导出一份 markdown 报告。
_避免使用：_ RLHF（没有任何东西在训练模型）。

## 标记的歧义

### "Workflow"

在 Engine 中不是一等概念。用确定性 Policy + `spawn_subtask` 表达固定过程。模型即兴创作的编排脚本表现为**一个 Task + 一个解释该脚本的 Policy**。（平台的 *workflow session* 是应用层对根任务的顺序编排，不是 Engine 原语。）

### "Session"

一个**仅存在于应用层的概念**（见上文）。在应用层之下它仍然不是概念：Engine 只认识 Task，多轮对话就是一个 Task 反复接收用户输入。

### "Run"

不是一等概念。始终使用 Task。
