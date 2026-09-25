# 术语表

Noeta 用到的术语，按字母排，每条一两句话，后面链到详细讲它的页面。以
[`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md) 为准。

## A

- **Activation（启用）**：一个 agent 用哪些已加载的插件，写在 `Options.plugins`。它算 agent 身份的一部分，写了不认识的名字会编译失败。→ [Options](options.md)
- **Agent**：有名字、可被派生的一份配置（`AgentSpec`）：指令、policy、工具、技能、预算、插件。相当于 task 的"类"，本身不在运行。→ [预设](presets.md)
- **Anchored placement（按启用位置摆放）**：常驻内容在哪启用就摆在哪：第一条 assistant 消息之前启用的放在半稳定段，之后启用的就地插进历史。→ [上下文](../how-it-works/context.md)
- **App plugin**：挂在宿主自己扩展点上的贡献（路由、渠道、定时任务）。加载器照样校验，交给宿主处理，不算 agent 身份。→ [插件扩展点](plugin-surfaces.md)
- **Artifact**：工具在正文输出之外返回的大对象，以 `ContentRef` 列在 `ToolResult.artifacts` 上。→ [类型](types.md)
- **Attempt**：一个 step 里"决定再执行"的一轮，崩溃恢复就按它算。中断的那一轮用 `StepAttemptAbandoned` 封掉。→ [事件日志](../how-it-works/event-log.md)

## B

- **Backend bag**：`SessionBuildContext` 上的 `backends` 映射，按插件名放活的后端对象（`"browser"`、`"app_preview"`）。没这一项，对应能力就不开。→ [插件扩展点](plugin-surfaces.md)
- **浏览器工具**：`browser_navigate`、`browser_click`、`browser_type`、`browser_extract`、`browser_screenshot`。要有活的浏览器后端，且 agent 启用了 `browser`。不走 MCP。→ [内置工具](tools.md)
- **Budget（预算）**：资源上限：`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks`、`max_subtask_depth`。`None` 表示不限；计数覆盖 task 整个生命周期，不按轮清零。→ [Options](options.md)
- **内置插件**：Noeta 自带的 18 个能力，放在 `noeta/builtins/` 下，和外部插件走同一条加载路径。`react` 不能关。→ [插件系统](../how-it-works/plugin-system.md)

## C

- **Content channel（常驻内容通道）**：常驻内容进上下文的方式：`ContextContentRecorded` 事件负责记录，`ContentKindSpec` 负责渲染。目前有 `skill`、`memory`、`instructions`、`environment` 四类。→ [上下文](../how-it-works/context.md)
- **ContentRef**：指向 `ContentStore` 的引用：`hash`、`size`、`media_type`，按 `hash` 查。→ [类型](types.md)
- **ContentStore**：按内容寻址、写了不改的大对象存储。协议成员是 `get` 和 `get_many`。→ [事件日志](../how-it-works/event-log.md)
- **上下文三段**：View 分三段：`stable_prefix`（系统提示词、工具 schema）、`semi_stable`（常驻内容）、`dynamic_suffix`（历史和提醒）。前缀每步必须逐字节不变，provider 缓存才命中。→ [上下文](../how-it-works/context.md)
- **ContextComposer**：用折叠出的状态和 `ContentStore` 拼出 View，不调模型。它本身不开放扩展，要加东西就注册内容类型或提醒。→ [上下文](../how-it-works/context.md)
- **ContextPlan**：每次调模型的元数据：选了哪些技能和消息、丢了或清了什么。存下来用于审计和排查。→ [上下文](../how-it-works/context.md)
- **Contract**：task 不可改的头信息，写在第一条 `TaskCreated` 事件里：`goal`、`policy_name`、`agent_name`、`inputs`、`parent_task_id`、`subtask_depth`。→ [任务与唤醒](../how-it-works/tasks-and-waking.md)
- **Control tool mount**：`control_tool` 贡献，工具集拼好之后再构造控制工具（`TodoWrite`、`skill`、`Task` 等）；返回 `None` 就是不挂。→ [插件扩展点](plugin-surfaces.md)

## D

- **Decision**：`Policy.decide` 的返回值：`tool_calls`、`spawn_subtask`、`spawn_subtasks`、`yield_for_human`、`wait_timer`、`wait_external`、`state_patch`、`compaction_requested`、`finish`、`fail`。→ [Engine](../how-it-works/engine.md)
- **Dispatcher**：负责调度：入队、发租约、投递唤醒、回收过期租约。task 状态从不从它这里读。→ [任务与唤醒](../how-it-works/tasks-and-waking.md)

## E

- **Engine**：把一个 task 推进一步（`run_one_step`）。主循环是写死的，不能扩展。→ [Engine](../how-it-works/engine.md)
- **Event / EventEnvelope**：日志里的一条记录：`seq`、`type`、`actor`、`origin`、`trace_id`、`causation_id`，加一个带类型的 payload。→ [类型](types.md)
- **EventLog**：每个 task 一条只追加的事件流，是唯一的事实来源。内联 payload 上限 4 KB（`EVENT_PAYLOAD_MAX_BYTES`）。→ [事件日志](../how-it-works/event-log.md)
- **ExecEnv**：文件和 shell 工具真正干活的后端：`LocalExecEnv`（本机）或 `AioSandboxExecEnv`（容器）。不出现在工具 schema 里。→ [沙箱](../guides/sandbox.md)

## F

- **Fork**：见"Rewind 和 fork"。

## G

- **Guard**：在调工具、派子任务、结束之前同步检查，返回 `allow`、`deny` 或 `require_approval`。guard 自己抛异常按 `deny` 算。→ [Engine](../how-it-works/engine.md)

## I

- **查看历史**：`Client.events` / `events_after` 返回原始事件，`Client.messages` 返回可读的对话。只读，不影响 task。→ [SDK](sdk.md)
- **Instructions discovery**：可选功能：agent 读了某个文件后，把它到工作区根目录之间各级的 `NOETA.md` / `AGENTS.md` / `CLAUDE.md` 加进上下文。默认关。→ [上下文](../how-it-works/context.md)
- **Interrupt（打断）**：只停掉正在跑的这一轮（`TurnInterrupted`），task 不结束，接着发消息就能继续。`force=True` 用来清掉卡死的一步。如果 task 正在等用户回答问题，打断会撤回这个问题。→ [SDK](sdk.md)

## L

- **Lease（租约）**：worker 对一个 task 的短期独占（`lease_id`、`task_id`、`expires_at`），靠心跳续期，过期被回收。每次写日志都要带上它。→ [Worker 循环](worker-loop.md)

## M

- **Memory（记忆）**：跨 task、存成文件、由模型自己管的记忆，工具是 `memory_write`、`memory_archive`、`memory_read`、`memory_search`。官方 agent 里只有 `main` 启用。→ [多租户记忆](../guides/multi-tenant-memory.md)
- **记忆整理**：后台 agent（`__consolidation__`）合并、归档、整理记忆库。作为独立的根 task 运行；只归档，不删除。→ [多租户记忆](../guides/multi-tenant-memory.md)
- **记忆召回**：每轮开始前，用户消息点到名字的记忆页自动带进来；模型已经读过的页会跳过。设了 `Options.recall_model` 还会用小模型再判断一次。→ [多租户记忆](../guides/multi-tenant-memory.md)

## O

- **Observer**：异步、只读地订阅事件日志，在提交后运行；它出错不影响 task。→ [Engine](../how-it-works/engine.md)
- **Options**：声明式的 agent 配置（`noeta.sdk.Options`），编译成一个 `AgentSpec` 和它的子 agent。→ [Options](options.md)
- **Origin**：消息是谁写的：`human`、`system` 或 `memory`（默认 `None`，即按角色本来的作者）。只有 Engine 能设；`system` / `memory` 的消息显示为 `InjectedMessage`。→ [类型](types.md)

## P

- **PackContribution**：session pack 的返回值：`tools`、`content_kinds`、`init`，外加几个带类型的附加字段。返回空就表示不适用。→ [插件扩展点](plugin-surfaces.md)
- **Plugin（插件）**：一个 pip 包或单个 `.py` 文件，带一份静态 manifest 说明它提供什么。读 manifest 不会执行插件代码。→ [写插件](../guides/plugins.md)
- **PluginSet**：`load_plugins(...)` 的返回值，传给 `Client(options, plugins=...)`。不跑插件代码就能审查；只有 `.resolve()` 会真正 import。→ [插件 manifest](plugin-manifest.md)
- **Policy**：根据 View 决定下一步（`decide(ctx, view) -> Decision`）。默认是 `ReActPolicy`，标识 `("react", "1")`。→ [Engine](../how-it-works/engine.md)
- **Principal**：谁在操作、能用哪些模型（`identity`、`allowed_models`）。日志里只记 `principal_identity`。→ [Options](options.md)
- **Provider**：对接外部服务的适配器。`LLMProvider` 通过 `Options.provider` 设置，各家适配器在 `noeta.sdk.providers`。→ [接入模型](../guides/models.md)

## R

- **提醒**：往上下文里加的提示文字。`reminder_provider` 在接收输入时运行并记进日志；`reminder` 是纯函数，只在历史末尾渲染，不记日志；常驻内容是第三种。→ [插件扩展点](plugin-surfaces.md)
- **Resume（恢复）**：让挂起的 task 继续：折叠日志，拿租约往下跑。由匹配的唤醒事件触发（`send_goal`、`approve` / `deny`、`answer`、`deliver_event`）。→ [任务与唤醒](../how-it-works/tasks-and-waking.md)
- **Rewind 和 fork**：都从某条用户消息分叉。rewind 在同一个 task 上追加 `TaskRewound`，并还原被改过的文件；fork 新开一个 task（`TaskForked`），原 task 不动。→ [SDK](sdk.md)

## S

- **SandboxProvider**：按根 task 创建和回收容器（`allocate`、`release`、`attach`），之后由 `ExecEnv` 和容器通信。→ [沙箱](../guides/sandbox.md)
- **Session pack**：`session_pack` 贡献，根据 `SessionBuildContext` 构造 task 的一部分工具和常驻内容；不适用时返回空。→ [插件扩展点](plugin-surfaces.md)
- **SessionBuildContext**：所有 session pack 读的只读输入：工作区、content store、exec env、模型、允许的工具、backend bag、插件配置。→ [插件扩展点](plugin-surfaces.md)
- **Skill（技能）**：放在 `.noeta/skills/<name>/SKILL.md` 的静态流程模板。模型先看到名字和摘要的清单，选中后才加载正文。不是工具。→ [预设](presets.md)
- **快照**：`TaskSnapshot` 事件，指向 `ContentStore` 里的完整状态，在每次挂起和结束前写。只为加快折叠，没有也能算出同样的状态。→ [事件日志](../how-it-works/event-log.md)
- **Step**：Engine 跑一遍：组上下文 → 决策 → 执行，有工具调用就接着转，直到挂起或结束。→ [Engine](../how-it-works/engine.md)
- **Subtask（子任务）**：由父 task 派生，用 `parent_task_id` 和 `subtask_depth` 关联，其余和普通 task 一样。→ [子 agent](../guides/subagents.md)
- **Surface / SurfaceSpec**：一个具名扩展点，以及对它的描述（类别、作用范围、校验、冲突键、顺序）。标准扩展点共 16 个。→ [插件扩展点](plugin-surfaces.md)
- **Suspended（挂起）**：task 在等某个唤醒条件（子任务、审批、定时、外部事件），等什么都是这一个状态。task 状态有 `pending`、`running`、`suspended`、`terminal`。→ [任务与唤醒](../how-it-works/tasks-and-waking.md)

## T

- **Task**：agent 的一次执行，系统里唯一的一等实体。能派子任务、挂起、恢复。→ [任务与唤醒](../how-it-works/tasks-and-waking.md)
- **状态四块**：task 状态分四块，每块只有一个写入方：`RuntimeState`（Engine）、`TaskState`（Policy 的 `state_patch`）、`ContextState`（折叠）、`GovernanceState`（折叠）。→ [事件日志](../how-it-works/event-log.md)
- **TaskState**：存 task 自己工作记忆的那块：目标、阶段、待办、决定、常驻内容。只属于这个 task，和跨 task 的 Memory 不同。→ [事件日志](../how-it-works/event-log.md)
- **Tool（工具）**：agent 能调的动作：`name`、`input_schema`、`description`（模型看到的说明），加上 `version` 和 `risk_level`。不是技能。→ [内置工具](tools.md)

## V

- **View**：composer 给 policy 拼好的模型输入，是 task 的投影，不是 task 本身。→ [上下文](../how-it-works/context.md)

## W

- **WakeCondition / WakeEvent**：task 在等什么、等来了什么：`SubtaskCompleted`、`SubtaskGroupCompleted`、`HumanResponseReceived`、`TimerFired`、`ExternalEvent`。持久投递，恰好一次。→ [任务与唤醒](../how-it-works/tasks-and-waking.md)
- **Worker**：拿租约驱动 task 的进程，一直跑到下一次挂起或结束。循环本体是 `noeta.runtime.worker.WorkerLoop`。→ [Worker 循环](worker-loop.md)
- **写入范围限制**：`Edit` 和 `Write` 只能写工作区或宿主放行的目录（`HostConfig.write_roots`）。读不受限，`Bash` 也不受限。→ [Options](options.md)

## 不用的词

`scripts/lint-naming.py` 会拒绝类名 `Run`、`Workflow`、`Session`、`Mutator`、`Pattern`，
以及标识符 `WorkflowRunner`、`WorkflowPolicy`、`WorkflowSpec`、`SessionStore`、`ConversationManager`。

- **Run**：一律说 Task。
- **Session**：不作为身份。一段对话就是一个 Task 反复收到新目标；用 `task_id` 或 `root_task_id` 指代。"在一棵根 task 树的生命周期内"这种范围说法可以用。
- **Workflow**：不是基础概念。用确定性的 Policy 加 `spawn_subtask` 决策来写。

## 下一步

- [工作原理](../how-it-works/index.md)
- [SDK 参考](sdk.md)
