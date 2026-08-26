# 术语表

Noeta 用到的每个术语，只定义一次。每条词目都是一段平实的简短定义，加上一个指向完整讲解页面的指针。权威来源是仓库根目录的 [`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md)；本页是它的发布形态。

术语按领域分组排列。用下面的索引可以直接跳到某一条。

## A–Z 索引

**A** [Activation](#activation) · [Agent](#agent) · [Anchored placement](#anchored-placement) · [App plugin](#app-plugin) · [Artifact](#artifact) · [Attempt](#attempt)

**B** [Backend bag](#backend-bag) · [Browser tool pack](#browser-tool-pack) · [Budget](#budget) · [Built-in plugin](#built-in-plugin)

**C** [Content channel](#content-channel) · [ContentRef](#contentref) · [ContentStore](#contentstore) · [Context segments](#context-segments) · [ContextComposer](#contextcomposer) · [ContextPlan](#contextplan) · [Contract](#contract) · [Control tool mount](#control-tool-mount)

**D** [Decision](#decision) · [Dispatcher](#dispatcher)

**E** [Engine](#engine) · [Event and EventEnvelope](#event-and-eventenvelope) · [EventLog](#eventlog) · [ExecEnv](#execenv)

**G** [Guard](#guard)

**I** [Inspect](#inspect) · [Interrupt](#interrupt)

**L** [Lease](#lease)

**M** [Memory](#memory) · [Memory consolidation](#memory-consolidation)

**O** [Observer](#observer) · [Options](#options) · [Origin](#origin)

**P** [PackContribution](#packcontribution) · [Plugin](#plugin) · [PluginSet](#pluginset) · [Policy](#policy) · [Principal](#principal) · [Provider](#provider)

**R** [Reminder tracks](#reminder-tracks) · [Resume](#resume) · [Rewind and fork](#rewind-and-fork) · [Run](#run)

**S** [SandboxProvider](#sandboxprovider) · [Session](#session) · [Session pack](#session-pack) · [SessionBuildContext](#sessionbuildcontext) · [Skill](#skill) · [Snapshot](#snapshot) · [Step](#step) · [Subtask](#subtask) · [Surface and SurfaceSpec](#surface-and-surfacespec) · [Suspended](#suspended)

**T** [Task](#task) · [Task state slices](#task-state-slices) · [TaskState](#taskstate) · [Tool](#tool)

**V** [View](#view)

**W** [WakeCondition and WakeEvent](#wakecondition-and-wakeevent) · [Worker](#worker) · [Workflow](#workflow) · [Write fence](#write-fence)

## 核心模型

### Task

一个 agent 的一次执行实例，也是系统中唯一的一等公民。它可以派生子任务、在一个唤醒条件上挂起、以及恢复。`Task` 对象本身很小——`task_id`、`status`、四个状态切片——因为它的历史和快照都住在 EventLog 与 ContentStore 里。
→ [任务模型](../concepts/task-model.md)

### Subtask

由一个 `spawn_subtask` 决策从父任务派生出的任务。结构上与其他任何任务完全相同，只通过 `parent_task_id` 加上 `subtask_depth`（递归预算所限制的委派深度）关联。
→ [生成子代理](../how-to/spawn-subagents.md)

### Agent

一个具名的、可派生的配置——一个 `AgentSpec`，携带指令、policy 与 composer 的 ref、工具、skill、预算、被激活的插件，以及它可以派生的名字。**不是运行时实体**：它是一个任务的"类"，只有身份，并且经过归一化，因此两份仅在作者书写顺序上不同的 spec 比较相等。子 agent 的 `description` 必填且非空，因为它会被渲染进 `Task` 的 schema，好让模型知道该委派给谁。
→ [预设代理](presets.md)

### Options

声明式的 agent 配方（`noeta.sdk.Options`），由 `compile_options` 编译成一个 `AgentSpec` 加上一个 `Client` 会去注册的扁平后代 spec 元组。它是表达官方 agent 和自定义 agent 的唯一方式，而且编译是纯的：相等的配方编译出相等的 spec。
→ [Options](sdk-options.md)

### Contract

一个任务不可变的头部，在创世的 `TaskCreated` 事件里冻结，从不被改写：`goal`、`policy_name`、`agent_name`、`inputs`、`parent_task_id`、`subtask_depth`。每一次 fold 都从它引导出空状态。一个大到超过 4 KB 负载上限的目标会溢出到 ContentStore，经由 `goal_ref` 取回。

### Budget

一个任务的资源上限——`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks`、`max_subtask_depth`。某个字段为 `None` 表示那一维不设上限。哪些上限适用取决于动作：一次工具调用检查全部，一次派生跳过 `max_tool_calls`，而一次 finish 只检查历史累计量，因此一个仅仅耗尽了工具预算的任务仍然可以给出答案。

### Principal

谁在行动，以及这个身份可以绑定哪些模型：一个 `identity` 字符串加上一个 `allowed_models` 集合，用 `allows_any=True` 表示无界集合。驱动器会在发出任何 `ModelBound` 之前，把一个选择器同时对着 principal *和*部署白名单校验。Principal 从不进入事件负载——随行的只有 `principal_identity` 字符串，作为回溯"谁批准了这次绑定"的审计链接。

### Step

一个任务在 Engine 主循环的一次通过中前进的那个片段：`compose → decide → dispatch`。一个 `tool_calls` 决策让循环原地继续转——追加结果、重新组装、再问一次——而任何其他决策都会在一次挂起或一个终止处结束这个 Step。
→ [引擎与执行](../concepts/engine-execution.md)

### Attempt

一个 Step 内部的一次"决策—行动"迭代，也是崩溃恢复的单位。它的第一条持久记录是 `ContextPlanComposed`；一个 `StepAttemptAbandoned` 标记把一次被中断的 attempt 封存为被折叠过去的死历史。
→ [ADR: Step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md)

### Decision

`Policy.decide` 的返回值，也是 Engine 分发的输入。这些变体是刻意中立的机制：`tool_calls`、`spawn_subtask`、`spawn_subtasks`、`yield_for_human`、`wait_timer`、`wait_external`、`state_patch`、`compaction_requested`、`finish`、`fail`。产品级的 control tool 不会有自己的变体——`TodoWrite` 和 `skill` 表达为 `state_patch`，`AskUserQuestion` 表达为 `yield_for_human`。
→ [引擎与执行](../concepts/engine-execution.md)

### Policy

在给定当前 View 的情况下决定下一步的那个函数（`decide(ctx, view) -> Decision`）。它可以是一个纯 LLM（`ReActPolicy`）、一个纯状态机，或者一个混合体。每个编译出的 `AgentSpec` 都钉住一个 policy 身份；默认是 `("react", "1")`，而 `policy` Surface 就是换上另一个大脑的方式。
→ [插件 Surface](plugin-surfaces.md)

### Tool

agent 可以调用的一个外部动作。`name` / `input_schema` / `description` 这个三元组是手写的、面向 LLM 的——`description` 是模型所见内容的唯一事实来源，绝不在系统 prompt 里重复。一个工具还携带 `version` 和 `risk_level`，后者决定审批门控。**不是 Skill。**
→ [内置工具](tools.md)

### Skill

一个本地的、静态的 LLM 工作流模板，位于 `.noeta/skills/<name>/SKILL.md`，可以附带资源文件。层级由低到高合并：内置/插件/借入目录（`extra_skill_dirs`，如 `~/.claude/skills`，选择加入）、全局 `~/.agents/skills`（选择加入）、全局 `~/.noeta/skills`（选择加入）、工作区 `.agents/skills`，最后是工作区 `.noeta/skills`——同一作用域内厂商专属目录胜出，默认只挂载两个工作区层。加载分两阶段——*菜单*（名字加一行摘要）被渲染进 `skill` control tool 的 schema，而只有被选中的那个 skill 的正文才会进入半稳定段，压缩不会把它冲掉。捆绑的资源按需取用：渲染器会在前面加上 `Base directory for this skill: <dir>`，模型再用 `Read` 去读文件。**不是 Tool。**

### Provider

一个 Noeta 形状的外部服务适配器；每种服务实现对应的内部协议。`LLMProvider` 通过 `Options.provider` 开放，并从 `noeta.sdk` 重新导出；适配器和模型目录住在 `providers` 内置项里，经由 `noeta.sdk.providers` 获取。存储后端则走宿主配置。**不是上下文内容来源**——内容只有先被记录、再被渲染，才会进入上下文。
→ [Provider 中立](../concepts/provider-neutrality.md)

## 执行

### Engine

把单个任务推进一步，其中一步是一个轮次边界：`run_one_step` 在内部对 `tool_calls` 决策循环，并在下一次挂起或终止处返回。它既不依赖 Worker 也不依赖 Dispatcher。主循环是**封闭的**——不是一个扩展点。不要把 Engine 这个类和 `noeta-runtime` 这个 wheel 混为一谈。
→ [引擎与执行](../concepts/engine-execution.md)

### Worker

从 Dispatcher 租用一个任务、并调用 Engine 推进它的那个进程。**一个租约一直运行到下一次挂起或终止状态，然后释放。** 排空循环作为库原语 `noeta.runtime.worker.WorkerLoop` 交付；没有东西替你启动它。
→ [WorkerLoop](worker-loop.md)

### Lease

一个 worker 对一个任务的短期独占持有——`lease_id`、`task_id`、`expires_at`——由 `heartbeat` 延长，过期后由 `requeue_stale` 回收。worker 在每一次 EventLog append 上都出示 `lease_id`，单写者不变式正是这样被强制的。
→ [状态与写入者](../architecture/state-and-writers.md)

### Dispatcher

调度组件：任务入队、租约授予、唤醒投递和过期回收。任务状态从不从它那里读回来——生产代码 fold 的是 EventLog，那才是唯一的事实来源。
→ [唤醒与恢复](../concepts/wake-resume.md)

### Suspended

任务四种状态（`pending`、`running`、`suspended`、`terminal`）之一：停在一个唤醒条件上。它是"在等一个子任务、一次审批、一个定时器或一个外部事件"的统一表达——无论原因是什么，都只有一个状态和一条恢复路径。
→ [唤醒与恢复](../concepts/wake-resume.md)

### WakeCondition and WakeEvent

一个任务在等什么：`SubtaskCompleted`、`SubtaskGroupCompleted`、`HumanResponseReceived`、`TimerFired`、`ExternalEvent`。条件与事件每个变体共用一个 dataclass——存在 `task.wake_on` 上时它声明所等待的形态，经由 `Dispatcher.wake` 投递时它携带答案。匹配是通过 `matches_wake` 做的标识字段投影，而每个 Dispatcher 都经由那个 helper 路由，因此没有适配器能私下产生分歧。投递是持久、单 worker、恰好一次的。
→ [唤醒与恢复](../concepts/wake-resume.md)

### Resume

继续一个挂起的任务：把它的流 fold 回状态、在一个租约下把它向前驱动、追加所发生的事情。触发条件是一个与 `wake_on` 匹配的唤醒事件；人这一侧的入口是 `send_goal`、`approve` / `deny`、`answer` 和 `deliver_event`。状态只来自 fold，因此一个被恢复的轮次绝不会重新执行账本里已经记录过的东西。
→ [query / Client](sdk-client.md)

### Interrupt

第三种人为停止。`cancel` 写入 `TaskCancelled`，对话进入终止；`close` 把它归档；而 **`interrupt`** 写入 `TurnInterrupted`，只停掉进行中的这一轮，任务停在它的下一目标挂起上，再打一次字就能重开。它落在轮次边界上，而这个边界会被迅速抵达：进行中的 LLM 轮次是一次可放弃的等待（任意阶段都能在毫秒级中止），重试退避按取消轮询切片，工具批次在调用之间轮询，前台 shell 也会像后台 shell 一样被回收。一个卡死到所有协作缝隙之外的工具调用则交给 `interrupt(force=True)`——双击 Esc 的升级路径：被卡住那一步的租约被强制清除，脏的尝试窗口由 step-attempt recovery 封印，任务落回被中断的挂起点。被中断那一轮的事件仍作为真实历史留在流上；把它们"收回去"是 rewind 的职责。

当 `interrupt` 落在一个挂起于待回答 `ask_user_question` 的任务上（此时没有进行中的轮次）时，它转而**撤回该问题**：写入 `UserQuestionWithdrawn`，用一个配对的 `success=False` 工具结果关闭悬空的 ask 工具调用，并把对话停在下一目标挂起、进入空闲——不驱动任何模型轮次。这就是 "Esc" 落点：问题浮层消失，此前那一轮的输出仍留在历史里，用户打字即可续。审批挂起仍以 `deny` 作为其优雅退出。

### Rewind and fork

两个分支动词，共用一个锚点——一条用户目标 `MessagesAppended` 的 seq——只在新基线落在哪里上有区别。**Rewind** 向同一条流追加 `TaskRewound`，因此被锚定的那一轮以及它之后的一切都成为被折叠过去的死历史（什么都不会被删除），那一段所编辑的工作区文件会被还原。**Fork** 向一个**新**任务的流追加 `TaskForked`，对源任务什么都不写，因此两个分支都活下来；一个 fork 是兄弟，不是子任务，而且它分叉的是对话，不是工作区。

### Guard

在三个点之一——`before_tool_call`、`before_spawn_subtask`、`before_finish`——上的一次同步检查，返回 `allow`、`deny` 或 `require_approval`。Guard 按 `priority` 升序运行，第一个非 allow 的裁决决定结果；一个 `check` 抛出异常的 Guard 会被转换成一个起决定作用的 `deny`，因此一个有 bug 的 Guard 绝不会悄悄放行一个动作。
→ [Guard 与 Observer](../concepts/guard-observer.md)

### Observer

一个订阅到 EventLog 的异步钩子。它的失败无法影响任务。回调在提交之后、并且在写者锁之外触发，因此 Observer 要自己守护自己的状态并吞掉自己的异常。Observer 是只读的——要改变行为，请改 Policy 或 Composer。
→ [Guard 与 Observer](../concepts/guard-observer.md)

### Write fence

**写**类 fs 工具（`Edit`、`Write`）解析时所经过的那个路径包含接缝：目标必须落在会话工作区之内，或者落在宿主授权的一个额外根之下。包含判定是按路径分量进行的（`path_within`），绝不是字符串前缀，因此 `/srv/app-old` 不在 `/srv/app` 之内。**读取不受围栏限制**，而那个用于放宽的 resolver（`HostConfig.write_roots`）在每一种退化情形下都 fail closed。这是一条"刻意改动"的边界，不是进程隔离——`Bash` 能触及整个文件系统。

### ExecEnv

fs 与 shell 工具借以行动的可插拔执行后端——工具与它们真正的 IO 之间的一个深接缝，作用在已经解析好的绝对路径上。`LocalExecEnv` 是宿主的文件系统与子进程；`AioSandboxExecEnv` 把每一个副作用都经 HTTP 路由到一个容器。它作为一个按工具的构造字段注入，**从不**是工具 schema 的一部分，因此无论绑定哪个后端，稳定前缀都字节相同。**和"sandbox"不是一回事**——sandbox 只是这个接缝的一个后端。
→ [使用 Sandbox](../how-to/use-sandbox.md)

### SandboxProvider

负责按会话开出并回收一个容器的那个接缝——它不同于 `ExecEnv`，后者是与一个已经在跑的容器对话。`allocate` 返回一个 `SandboxHandle`（寻址信息加上一个从不被序列化的活认证策略），`release` 在根任务进入终止状态时把它拆掉，而 `attach` 在恢复时重新连上一个被记录下来的 ref。开容器属于宿主，机制属于运行时，绑定属于 SDK——配置携带寻址信息，从不携带秘密。

### Browser tool pack

由 Noeta 拥有的那组浏览器工具（`browser_navigate`、`browser_click`、`browser_type`、`browser_extract`、`browser_screenshot`），一个处于 sandbox 中的 agent 用它们驱动容器里的无头浏览器。它同时受**两个**条件门控：一个活的浏览器后端，以及该 agent 激活了 `browser`。名字和 schema 都是 Noeta 的，因此稳定前缀从不依赖容器镜像。**不是一个 MCP 连接器**——容器的 MCP 端点在这里只是一种内部传输。
→ [内置工具](tools.md)

## 上下文与记忆

### View

ContextComposer 为 Policy 组装出的那份 LLM 输入。它是任务的一个*投影*，从来不是任务本身。

### ContextComposer

把一个任务组装成一个 View——`compose(task) -> View`。它不调用任何 LLM：它是 fold 状态加上 ContentStore 的纯函数。具体的 `ThreeSegmentComposer` 是一个**封闭的**扩展点，因为稳定前缀的缓存可复现性是一个硬约束。开放的钩子只走注册、且只能追加：注册一个 `ContentKindSpec` 或一个 compose 时的 `reminder`。
→ [Composer 与缓存](../concepts/composer-and-cache.md)

### ContextPlan

一次 LLM 调用的 View 元数据：选中了哪些 skill 和消息、丢弃或清理了什么、检索到了什么。正文写入 ContentStore，它的 ref 折进 `ContextState.plan_ref`。它的存在是为了审计和调试。

### Context segments

View 分三部分组装。`stable_prefix` 携带系统 prompt 消息和 provider 工具 schema；`semi_stable` 携带内容通道的常驻内容；`dynamic_suffix` 携带滚动历史，尾端跟着 compose 时的 reminders。让稳定前缀在两步之间字节可复现，是一个协议级的硬约束——扰动它会把 provider 的 KV 缓存炸掉，成本随之飙升。
→ [Composer 与缓存](../concepts/composer-and-cache.md)

### Content channel

常驻内容进入上下文的那套通用机制，分为两半。**记录**：一个 `ContextContentRecorded` 事件携带 kind、name、version、`content_hash` 和漂移策略；fold 设置 `active_content[kind][name]`，哈希后写者获胜，因此用新哈希重新记录一次就是一次刷新。**渲染**：每个 kind 一个 `ContentKindSpec`，而注册顺序*就是*半稳定段的布局。组装出的字节是 fold 状态加内容存储的纯函数。使用者与条带：`skill`（100）、`memory`（200）、`instructions`（300）、`environment`（400）。
→ [Composer 与缓存](../concepts/composer-and-cache.md)

### Reminder tracks

编写好的上下文文本抵达 View 的三种方式，按它们何时运行、以及是否被记录来区分。**轨道 A（`reminder_provider`）**会被记录、可以不纯，运行在一个摄入接缝上；恢复时 fold 从账本里把它的输出折回来，而不是重新调用它。**轨道 B（`reminder`）**发生在 compose 时且是**纯的**，渲染在动态后缀的尾端，从不被记录。**轨道 C** 是常驻内容通道。B 与 C 中第三方渲染的确定性是一个书面契约，不是被强制的。
→ [插件 Surface](plugin-surfaces.md)

### Anchored placement

一份内容通道常驻内容渲染在哪里，由它的激活锚点决定——即该次激活被 fold 时所记录的滚动历史长度，先写者获胜。一条规则，没有按 kind 的开关：锚点在第一条 assistant 消息处或之前的渲染在半稳定段；更晚的锚点则渲染在动态后缀里的那个位置上，因此一次任务中途的激活是追加，而不是改写头部。配套特性：**instructions discovery**，默认关闭，它会激活被读文件与工作区根之间各目录下的 `NOETA.md` / `AGENTS.md` / `CLAUDE.md`。
→ [ADR: Anchored content placement](https://github.com/initxy/noeta/blob/main/docs/adr/anchored-content-placement.md)

### Origin

`Message` 上一个可选的作者标记——`human`、`system` 或 `memory`——默认为 `None`，意思是这个 role 的自然作者。role 与 origin 是两个不同的维度：role 说的是这一轮走的是哪条通道，origin 说的是谁写的它。**单写者保护**：只有 Engine 的记录路径可以写它，而在模型或工具输出里伪造出来的标记只是文本。在 SDK 消息视图里，origin 为 `system` / `memory` 的消息投影为 `InjectedMessage`，绝不会是 `UserMessage`。

### TaskState

四个状态切片中，持有由 Policy 维护的长视野任务记忆的那一个——目标、阶段、todos、决策、活跃内容。这是长视野 agent 与短任务 agent 之间的核心差别。不要与 [Memory](#memory) 混淆，后者是跨任务的。

### Memory

跨任务的长期记忆：基于文件、由模型管理。改动走 `memory_write` 和 `memory_archive`（退役，绝不删除）；读取走 `memory_read` 和 `memory_search`。**常驻索引**是一个内容通道使用者，因此压缩绝不会把它冲掉，而**自动召回**是 `turn_intake` 接缝上的一个 provider：tier-1 命中会变成 `memory` 类别的常驻内容（一条记忆正文在一个 task 里只进入一次，activate-once，压缩后仍在），tier-2 命中则以指针行的形态落在一条 `origin="memory"` 的消息里。召回按字面 token 匹配（名字、摘要，以及 frontmatter 的 `keywords` 别名——确定性的跨语言桥梁）；设置 `Options.recall_model` 后，字面 miss 会经由一次小模型调用重试（**recall judge**：读消息加索引、挑出相关记忆），选中项以指针形态注入并照常落账；`memory_write` 会盖上 `created` / `updated` 日期和一条 `source_task` 账本回执。由 `plugins=("memory", …)` 激活，属于 agent 身份的一部分——在官方 agent 中只有 `main` 打开它。
→ [按租户隔离记忆](../how-to/multi-tenant-memory.md)

### Memory consolidation

对记忆存储的异步策展流程。一个保留名 agent（`__consolidation__`）作为普通根任务运行，被喂入一份近期活动的摘要，然后合并重复项、归档被取代的记忆、裁决记忆之间的互相矛盾、维护跨语言的 `keywords` 别名、补齐明显的缺口。它由宿主的停止接缝在一个防抖标记之后触发，从不被注入到一个活的任务里，而且它只能归档，绝不能删除。
→ [query / Client](sdk-client.md)

## 插件与扩展

### Plugin

一个由 manifest 声明的贡献包——一个 pip 包，或者一个单独的本地 `.py` 文件，携带一份静态 manifest，指明这个插件的名字、一个 `requires-noeta` 范围，以及它对各个 Surface 的贡献。这份 manifest 是惰性数据，读取它**不导入任何插件代码**：一条贡献的 `ref` 是一个字符串，只在 client 构建这个边界处才被解析。贡献确定性地合并；一次冲突会指名双方，且不存在覆盖。
→ [插件 manifest](plugin-manifest.md)

### PluginSet

由 `load_plugins(...)` 返回、并作为 `Client(options, plugins=…)` 传入的那个已加载的宿主级集合。它可以在不执行任何插件代码的前提下被列举与审计：`.contributions()` 和 `.merged()` 只读静态 manifest，而 `.resolve()` 是唯一的 import 边界，在 client 构建时调用，从不在某一轮里调用。
→ [插件 manifest](plugin-manifest.md)

### Activation

按 agent 选择它使用哪些已加载的插件——`Options.plugins` 和 `AgentDefinition.plugins`。激活*就是*身份：每个被识别的名字都折进 `AgentSpec.plugins`，而能力门控就是对那个元组做一次成员检查。一个名字要么是一个被识别的内置激活，要么是加载集里某个插件的名字；其他任何东西都会让编译大声失败。生效范围跟随 Surface——身份 Surface 跟随激活，而 guard 与 observer 不跟随。
→ [Options](sdk-options.md)

### Surface and SurfaceSpec

一个 **Surface** 是一个具名的扩展点；一个 **SurfaceSpec** 完整描述其中一个——它的平面、激活范围、校验器、冲突键、排序，以及（对身份平面的 Surface 而言）它的激活绑定。loader 与 Surface 无关：它只咨询一个 `SurfaceRegistry`，因此新增一个 Surface 意味着注册一个 SurfaceSpec，而不是去改 loader。跨三个平面共有十六个标准 Surface。
→ [插件 Surface](plugin-surfaces.md)

### Built-in plugin

Noeta 自身能力之一，以插件的形式表达，位于 `noeta/builtins/<name>/`：`__init__.py` 放零执行的 `MANIFEST`，`impl/` 放代码。内置项走的加载、校验和合并路径与任何外部插件完全相同，而这一整片区域只能通过动态 import 抵达——没有任何东西静态导入 `noeta.builtins`。共有十八个。`react` 是那个拒绝被禁用的：因为它提供每个 `AgentSpec` 都要钉住的默认 policy 身份。
→ [插件 Surface](plugin-surfaces.md)

### App plugin

宿主自己的 host 平面 Surface 上的一条贡献——路由、通道、调度、命令——由宿主在加载之前注册进 Surface registry。它由同一条流水线校验并查冲突，然后交还给宿主。从不是 `AgentSpec` 身份的一部分。

### Session pack

一项能力中负责会话构建的那一半：一条 `session_pack` 贡献，一个 `(SessionBuildContext) -> PackContribution` 工厂，由内核 builder 在一个按 priority 排序的循环里运行。builder 不按名字枚举任何能力。一个 pack 基于它的上下文**自我门控**，不适用时返回空贡献，因此内核里不为任何功能留一个 `if`。内置的条带由字节相等的 golden 锁定，因为工具插入顺序会喂进稳定前缀的哈希。
→ [插件 Surface](plugin-surfaces.md)

### SessionBuildContext

每个 session pack 都会读的那份通用冻结上下文：包含判定用的工作区根、工作目录、内容存储、exec env、模型与 provider 家族、允许的工具、backend bag、能力开关和插件配置。它在 pack 循环之前就构建好，因此没有哪个 pack 能扰动后一个 pack 的输入。它只携带通用槽位——一个只有单一消费者的旋钮，住在 `plugin_config` 下它所属插件的名字里。

### PackContribution

一个 session pack 交回来的东西：`tools`（按循环顺序合并，后者获胜）、`content_kinds`（各自带自己的注册优先级，因为布局顺序与工具顺序不同）、`init`（seed 时记录常驻内容的钩子），以及一小组带类型的旁路状态字段，每个恰好由一个内核接缝读取。所有字段都是可选的；空贡献就是通用的"不适用"答案。

### Backend bag

`SessionBuildContext` 上那个由宿主填充的 `backends` 映射——活的后端对象，按贡献它们的那些插件自己的名字作键（`"browser"`、`"app_preview"`），而绝不是内核的词汇。一个名字缺席就意味着这项能力没有活的后端支撑，于是那个 pack 返回空贡献。

### Control tool mount

一项能力中负责构建 control tool 的那一半：一条 `control_tool` 贡献，一个 `(ControlToolBuildContext) -> ControlToolMount | None` 工厂，在工具装配之后运行——因为一个 control tool 的 schema 是那些 pack 所产出的会话状态的函数。一个 mount 携带 `name`、`schema`、`translate`（一个闭包，捕获它自己的构建输入）以及两个由字节 golden 锁定的优先级——schema 渲染顺序和 translate 分派顺序。一个 mount 通过返回 `None` 自我门控；挂载*本身*就是启用。
→ [插件 Surface](plugin-surfaces.md)

## 存储与运维

### EventLog

一条按任务的只追加事件流。**因果与决策的事实来源。** 内联负载上限为 4 KB（`EVENT_PAYLOAD_MAX_BYTES`）；更大的正文住在 ContentStore 里。
→ [事件溯源](../concepts/event-sourcing.md)

### Event and EventEnvelope

一条 EventLog 流上的一条记录。信封持有 `seq` / `type` / `actor` / `origin` / `trace_id` / `causation_id`——`seq` 由日志在追加时分配——而负载是一个按 `type` 选定的类型化 dataclass。
→ [类型与测试](sdk-types.md)

### ContentStore

内容寻址的、不可变的大对象存储。**大对象的事实来源。** 读取有两种形状：`get`（单个 ref，缺失时抛出 `ContentNotFound`）和 `get_many`（一批，缺失的哈希会被略过，因此一份被回收的正文不会拖垮其余部分）；两者都是必需的协议成员。因为内容不可变，读缓存不需要任何失效规则。
→ [事件溯源](../concepts/event-sourcing.md)

### ContentRef

一个指向 ContentStore 的引用：`hash` + `size` + `media_type`。查找只按 `hash`。

### Artifact

一个 Tool 在它的内联输出之外产出的大对象，作为 `ContentRef` 列在 `ToolResult.artifacts` 上。

### Snapshot

一个 `TaskSnapshot` 事件，它的正文——完整的四切片任务状态——住在 ContentStore 里 `state_ref` 之后，在每次挂起和每个终止事件之前写入。它是 fold 的一个加速点；一次不用快照的 fold 会重建出同样的状态。
→ [Fold 与快照](../concepts/fold-and-snapshot.md)

### Task state slices

四个带类型的切片，**每个恰好一个写者**：`RuntimeState`（消息、用量——写者：Engine）、`TaskState`（目标、阶段、todos、决策、活跃内容——写者：Policy 的 `state_patch`，其中 `active_content` 由 fold 合并）、`ContextState`（plan ref、压缩摘要、每轮 thinking、内容锚点——写者：fold），以及 `GovernanceState`（成本、token 计数器、被拒绝的动作、子任务结果——写者：fold）。
→ [状态与写入者](../architecture/state-and-writers.md)

### Inspect

把一个任务的历史读回来。`Client.events` 和 `Client.events_after` 返回原始信封流；`Client.messages` 把它 fold 成人类可读的 View，并经 ContentStore 解引用大体积正文。纯读：没有外部 IO，对任务没有影响。
→ [query / Client](sdk-client.md)

## 关系

- **Task → Subtask** —— 一对多；一个子任务有自己的 EventLog 流，通过 `parent_task_id` 关联。
- **Agent → Task** —— 类与实例；一个 Agent 可以被许多 Task 实例化。
- **EventLog ↔ ContentStore** —— 配对；EventLog 持有决策与引用，ContentStore 持有大对象正文。
- **Engine ↔ Worker** —— 一对多；同一份 Engine 代码驱动每一个被租用的任务，Worker 把它包在租约与唤醒循环里。
- **Policy ↔ Tool** —— Policy *声明*一次调用，Engine *执行*它；Policy 从不直接调用 Tool。
- **Content channel ↔ Skill / Memory** —— 机制与使用者；新增一个使用者只需要注册一个 `ContentKindSpec`。

## 标记的歧义词

有三个词在别处另有含义，而 Noeta 不使用那些含义。这条禁令由 `scripts/lint-naming.py` 强制：它会在出现类名 `Run`、`Workflow`、`Session`、`Mutator`、`Pattern`，或标识符 `WorkflowRunner`、`WorkflowPolicy`、`WorkflowSpec`、`SessionStore`、`ConversationManager` 时让构建失败。

### Workflow

不是引擎里的一等概念。把一个固定流程表达为一个确定性 Policy 加上若干 `spawn_subtask` 决策。模型临场编出的编排脚本也不是一个新原语：它落成一个 Task 加上一个解释那个脚本的 Policy，而它派出的每个助手都是真正的 Subtask。根任务之间的多节点编排是宿主在上层构建的东西。

### Session

在这些库里不是一个身份。引擎只认识 Task，而一场多轮对话就是一个 Task 反复接收用户输入——每个问题是一*轮*，每次委派是一个 *Subtask*。把多轮归拢成一个用户可见 session 的宿主，自己拥有那个概念。

这条界线画在身份与范围之间，而只有一侧被禁止。**身份被禁止**：绝不要以 session 来命名一个东西，因为那个概念本来就已经有名字了——任务用 `task_id`，委派树的根用 `root_task_id`。**范围是允许的**，而且是真实的词汇："在一棵根任务树的生命周期内"是一句合法的表述，无论在散文里还是在 session pack 的构建词汇里。一个 session pack 构建的是一个任务的工具集——那是一个范围，不是一个身份。

### Run

不是一个一等概念。永远用 Task。

## 下一步

- [概念](../concepts/index.md) —— 同样这些想法，是解释而不是定义
- [SDK 参考](sdk.md) —— 每个术语在 API 里出现在哪里
- [架构概览](../architecture/overview.md) —— 各部分如何拼起来
