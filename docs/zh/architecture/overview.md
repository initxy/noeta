# 架构概览

自顶向下地走读 Noeta：两个包如何分层堆叠，事件溯源的核心如何塑造每一层，以及扩展 Surface 位于何处。对于"X 是什么"这类问题，本页会链接到[概念页面](../concepts/event-sourcing.md)；如需精确的 API 签名，请参阅[参考页面](../reference/sdk.md)。

## 两个包

Noeta 以两个库的形式发布：一个薄客户端位于一个纯引擎之上。

| 包 | 位置 | 角色 |
| --- | --- | --- |
| `noeta-runtime` | `packages/noeta-runtime` | 纯内核：`protocols`（唯一带类型的边界）、`core`（Engine、fold、snapshot）、内核服务（Worker、Dispatcher、工具运行时、内存版参考存储、Observer、读模型）、物料机制（`context` —— 锁定的 composer 与各注册表；`policies` —— 控制带；`tools` —— 编写机制）、仅供注入的 `execution` builder，以及 `agent` 身份层。不携带任何能力实现，也不含 HTTP 客户端。 |
| `noeta-sdk` | `packages/noeta-sdk` | 唯一的公共 Surface —— `query` / `Client` / `Options` / `@tool`、重新导出的扩展接口，以及各预设 agent —— 外加 `builtins`，即每个官方能力真正落脚的目录。 |

<p align="center">
  <img src="../../assets/architecture.svg" alt="Noeta 架构 —— 两个发行包与模块关系" width="820">
  <br>
  <em>host 在进程内驱动 SDK；SDK 转发进 runtime 的引擎、物料与存储。箭头为调用路径。</em>
</p>

两个包都向一个共享的 PEP 420 `noeta.` 命名空间贡献子包，因此即便发行包边界移动，导入路径也保持不变。依赖方向不靠自觉约束 —— import-linter 在 CI 中强制执行（`.importlinter`）：内核不得导入任何 provider 或后端适配器，而 `noeta-sdk` 在进程内转发进 runtime。用户只 import `noeta.sdk`；`noeta-runtime` 作为一个他们从不触碰的传递依赖到达。

## 内核不携带任何能力

每个官方能力 —— fs 与 web 工具包、provider 适配器、Guard、memory、browser、app、MCP、sandbox 后端、skills，以及 ReAct policy —— 都是 `packages/noeta-sdk/noeta/builtins/<name>/` 下的一个 **built-in plugin**。一个 built-in 在其 `__init__.py` 中声明一个零执行的 `MANIFEST`，并把代码放在 `impl/` 下；没有任何东西静态 import `noeta.builtins`。唯一的入口是插件加载器的动态 `ref` 解析，而 `.importlinter` 会拒绝任何静态 import —— 这也正是让内核永远够不到某个厂商适配器的机制，因为所有适配器都住在 `noeta.builtins` 里。

该目录持有十八个 built-in：`fs`、`web`、`memory`、`browser`、`app`、`mcp`、`skills`、`react`、`reminders`、`governance`、`providers`、`sandbox`、`presets`、`workspace`、`storage`、`todo_write`、`ask_user_question`、`delegation`。

## 事实基础：state = fold(log)

一个 Task 的事实基础是它的 append-only `EventLog`；任意时刻的状态都通过 fold 该日志计算得出，而从不作为一等副本存储。这一概念及其后果在[事件溯源](../concepts/event-sourcing.md)与 [Fold & snapshot](../concepts/fold-and-snapshot.md)中有详细说明。有两个架构级机制让这一承诺在实践中成立。

### 四个状态切片，各有唯一写入者

如果任何东西都能不经事件就改变状态，那么 fold 的重建结果将不再匹配实际运行的内容。因此 Task 状态被切分为四个带类型的切片，每个切片恰好有一个写入者（`packages/noeta-runtime/noeta/protocols/task.py`）：

| 切片 | 唯一写入者 | 内容 |
| --- | --- | --- |
| `RuntimeState` | Engine | 滚动的对话消息流、每轮用量、上一次转移、上一次输入 token 计数 |
| `TaskState` | Policy —— 仅通过 Decision 中的 `TaskStatePatch` | 待办事项、决策记录、已激活的 skill |
| `ContextState` | Composer | 上下文计划 ref、压缩摘要、剥离出的思考内容 |
| `GovernanceState` | fold，从事件累积 | 成本、迭代计数、token 计数、子任务结果 |

最能说明问题的一格是 `TaskState`：Policy 不能直接赋值给自己的长程记忆。它把一个 `TaskStatePatch` 附加到它返回的 Decision 上；Engine 将其落地为一个事件；fold 再把它写回。信封还携带一个 `origin` 标记，记录是哪个角色写入了它（`engine`、`llm`、`tool`、`observer`、`system`），而 Policy 合成的消息在进入消息流之前会被清除其 orig（`strip_message_origin`），因此它无法冒充另一个写入者。

### 跨版本 fold 旧记录

事件载荷与状态切片会演进，但很久以前挂起的一个 Task 仍必须能在当前代码下 fold。规范渲染层（参见 [Fold & snapshot](../concepts/fold-and-snapshot.md)）用两条对称规则来承载这一点：

- **添加字段不得破坏旧记录。** 新字段追加在其切片末尾、赋予默认值，并在为空时从字节流中省略 —— 因此旧记录（从未有过该字段）与当前代码（把它 fold 为默认值）保持字节相等。
- **移除字段不得让旧 snapshot 崩溃。** 恢复一个 snapshot 时，当前版本不再识别的键会被过滤掉，而不是传给一个会拒绝它们的构造函数。

一条规则保证"相同的当下 fold 出相同的字节"；另一条容忍"由不同版本写下的过去"。当某个 snapshot 完全早于必需字段时，fold 会丢弃它并从头重放 —— 更慢，但永不出错。

## 执行栈

### Engine

Engine 将一个 Task 推进一步 —— [compose → decide → dispatch](../concepts/engine-execution.md) —— 且对 Worker、Dispatcher 或 HTTP 一无所知。它的控制流只负责路由 Decision；实际的工作 —— 发出信封、运行工具、生成子任务 —— 都委托给外围处理器，从而让类体保持精简、易读。

### Worker、Dispatcher、Lease

Dispatcher 负责调度：任务入队、Lease 授予、唤醒投递，以及过期回收。Worker 驱动循环：

1. `dispatcher.lease(worker_id=…)` 返回一个 `Lease(lease_id, task_id, expires_at, wake_event=None)` —— 对一个 Task 的独占、由心跳续约的持有权。
2. Worker 将 `EventLog` fold 成一个 `RuntimeState`。
3. 如果设置了 `lease.wake_event`，Worker 调用 `engine.note_woken(…)`，它写入一个持久化的 `TaskWoken` 信封。
4. Worker 调用 `engine.run_one_step(task, lease_id=…)`，它把 Task 推进到下一个挂起或终止点 —— 内部对 `tool_calls` 决策循环，因此一次调用覆盖的是一整轮，而非单次模型往返。
5. Worker 调用 `dispatcher.release(lease_id, next_state=…, wake_on=…)` —— 或在意外异常时调用 `dispatcher.fail(…)`。

单写入者不变式在此以机械方式强制执行：`EventLog` 在每次 `emit(lease_id=…)` 时都会咨询 Dispatcher（作为 `LeaseRegistry`），因此只有持有活跃 Lease 者才能写入某个 Task 的消息流。Observer 在每个信封提交后同步看到它，运行在写入者线程上但在写入者锁之外，异常会被吞掉。

排空循环作为库原语 `noeta.runtime.worker.WorkerLoop` 提供 —— 没有东西替你启动它；嵌入的 host 自行调用 `WorkerLoop(…).run_forever(…)`（参见 [WorkerLoop 参考](../reference/worker-loop.md)）。

### 持久化唤醒

[唤醒与恢复](../concepts/wake-resume.md)陈述了这一保证 —— 持久化的恰好一次投递。机制如下：

- Dispatcher 通过投影把一个传入的唤醒事件匹配到一个挂起的 Task，并持久化保存该匹配。投递在 lease 时通过 `Lease.wake_event` 发生。
- Worker 把唤醒事件穿入 `engine.note_woken`，后者在这一步继续之前写入 `TaskWoken(wake_event=…)`。这次写入就是持久化提交点。
- 匹配**在 lease 之后仍然存活**：它只由一次消费性的 `release(consumed_wake_event=…)` 清除。Worker 在 lease 与 `TaskWoken` 写入之间崩溃，会把唤醒留在原位；`requeue_stale()` 把 Task 返回就绪，下一次 lease 会重新投递同一个唤醒。
- 消费是幂等的。Worker 的 woken 分支是一个以最新匹配的 `TaskWoken` 信封为键的恢复状态机：一次重新投递，若其 `TaskWoken` 已经落地，则会被协调而不发出第二个。
- 对一个没有排队唤醒的挂起 Task 尝试恢复会报告一个带类型的 `suspended_without_wake_event` —— 这是一个诊断信息，意为"在等待某件尚未发生的事"，而非故障。

该保证在并发 Worker 之间也成立：每一次经过 lease 校验的 append 都被 fencing，因此一个 lease 已被回收的滞留 Worker 无法把写落在接手它的那一代之后。单主机多 worker 在每种后端上都能运行；多主机在 Postgres 上运行，那里的栅栏是一次事务内的 `FOR SHARE` 行检查，并针对数据库时钟求值（把各主机的时钟偏差从决策中剔除）。SQLite 与内存版是单主机的。

Step 中途的崩溃会在下一次 lease 时恢复：被中断的 Attempt 会被密封，在无副作用时自动重新驱动，否则该 Task 会被停放等待人工处理。恢复范围、SQLite 边界，以及那一个开放边缘（sandbox 副作用在 Worker 代际之间不受 fencing 保护）都在[已知限制](../operations/limitations.md)中有编目。

## 上下文组装

每一步，`ThreeSegmentComposer` 从 fold 后的状态按波动性排序的三个段（`stable_prefix`、`semi_stable`、`dynamic_suffix`）组装模型的 View，让前缀保持字节稳定以利于 provider 的 KV-cache 复用；压缩是一个被记录的事件，而非就地编辑。该设计在 [Composer & cache](../concepts/composer-and-cache.md)中有详细说明。有一个准确性细节属于此处：是否应触发压缩，是拿 provider 为上一步报告的真实输入 token 数（已 fold 进 `RuntimeState.last_input_tokens`）来判断的，只有新追加的消息才做估算 —— 字符计数启发式会系统性地低估那些携带 cache、结构化块或图像的提示。

## Provider 边界

Engine 说一种中立的内部协议；厂商适配器在边缘做翻译、把厂商错误 fold 成一个中立分类（transient / context-overflow / fatal），并把诸如 cache 断点这类仅在线协议层面的机制挡在账本之外。三个适配器作为 `providers` built-in 发布：一个 Anthropic 适配器、一个面向任意兼容网关的 OpenAI `/chat/completions` 适配器，以及一个 OpenAI Responses 适配器。"内核不得导入 provider"这条规则让这个边界成为结构性的。参见 [Provider 中立](../concepts/provider-neutrality.md)。

## SDK Surface

`noeta.sdk` 是那个薄客户端：构建一个 `Options`，然后用 `query`（单轮）或 `Client`（多轮）在进程内驱动一个 agent。承重的设计是对 `Options` 各字段的一次切割：

- **身份字段**决定 agent 如何思考 —— 系统提示、skills、工具集、已激活的插件、一个自定义 Policy。它们进入记录，并在 fold 时被逐字复现。
- **接线字段**只把 agent 挂载到某个 host 上 —— provider 实例、工作目录、一个审批回调、Observer。它们被排除在身份之外（`compare=False`），因此交换它们不会扰动记录。

这次切割是强制性的，因为记录必须可复现：把两者混起来，一条记录就会因为工作目录变了而无法对齐。

以下是开放供扩展的部分，全都是通过 `noeta.sdk` 重新导出的 `Options` 字段：

| 字段 | 扩展内容 |
| --- | --- |
| `policy` | 把 ReAct 这颗大脑换成你自己的决策函数（携带一个 `.ref` 以让身份保持确定性） |
| `guards` | 效果发生之前的同步检查（参见 [Guard vs Observer](../concepts/guard-observer.md)） |
| `observers` | 只读的事件订阅者 —— 审计、指标 |
| `content_channels` | 注册一个 `ContentKindSpec`，把自定义的常驻内容放进半稳定段 |
| `mcp_servers` | 进程内的 SDK MCP 工具，或指向外部 stdio / HTTP MCP server 的连接器 |
| `@tool` | 给一个函数打上名称、版本、风险级别和输入 schema，使其成为一等工具 |

保持锁定的部分：Engine 主循环、Dispatcher / Worker / Lease 机制（host 只调节并发和 lease 时序），以及 `ThreeSegmentComposer` —— 整体替换 composer 不在用户 Surface 上，因为稳定前缀的可复现性是一条硬约束；它唯一开放的钩子是内容通道。存储后端通过 `HostConfig` 而非 `Options` 接线，并且从不进入 agent 身份；那条接线的公共入口是 `noeta.sdk.storage`。

一个 agent 会拿到完整的 built-in 工具集（11 个工具），除非被 `allowed_tools` / `disallowed_tools` 收窄，而 `permission_mode`（`default` / `acceptEdits` / `bypassPermissions`）决定高风险工具是否先询问。精确的签名见 [SDK 参考](../reference/sdk.md)。

## 插件 Surface

在 `Options` 各字段之下，插件加载器只咨询一个 `SurfaceRegistry`，别无其他，因此新增一个扩展 Surface 意味着注册一个 `SurfaceSpec`，而绝不用改动加载器。标准目录持有跨三个平面的十六个 Surface：

- **身份**（进入持久化的 agent 身份）：`tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool`。
- **接线**（挂载到某个 host 上，不进入身份）：`guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack`。
- **host**（由 host 接线的资源）：`mcp_server`、`skills`、`sandbox_provider`。

一个 host 通过取 `standard_registry()` 的一份 `copy()`、在调用 `load_plugins` 前注册自己的 Surface 来扩展这个集合。参见[插件参考](../reference/plugins.md)。

## agent 层

一个 agent 的身份是一个 `AgentSpec` —— 一个名字加上身份侧的配置（指令、policy ref、工具、`plugins` 激活元组、`spawnable` 允许列表）—— 从 `Options` 编译而来，并收集进一个注册表。身份层位于 runtime 的低层，只依赖协议层。

四个预设随附刻意收窄的 Surface：

| 预设 | 角色 | 工具 Surface | 可委派？ |
| --- | --- | --- | --- |
| `main` | 对话式控制器 | 完整 built-in + `todo_write` / `ask_user_question` / `skill_invocation` / `memory` / `mcp` | 是 |
| `general-purpose` | 自包含的编码工作者 | 读 / 写 / 编辑 + shell + web | 否 —— 一个叶子 |
| `explore` | 只读的侦察兵 | 仅只读工具 | 否 |
| `plan` | 只读的规划者 | 仅只读工具 | 否 —— 产出一份计划 |

那把收窄的刀是**激活（activation）**：写进 agent 身份的显式开关，即 `AgentSpec.plugins` 元组加上 `spawnable`，而非事后加装的运行时限制。功能门控通过 `agent_activates` 读取该元组 —— 成员资格*就是*能力本身。

协作有两种形态。**单次委派**：父方生成一个 Subtask，挂起，并在其完成时唤醒。**扇出**：父方生成一组 Subtask，它们并发运行在一个有界的进程内线程池上（上限取 8 与 CPU 数中的较小值），结果一起回流 —— 每个结果通过一个唤醒事件返回，并被配对到原始的那次工具调用。每个 Subtask 都是一个完整的、事件溯源的 Task，有自己的日志和 fold，仅通过 `parent_task_id` 与其父方关联；更精巧的编排被表达为一个 Task，其 Policy 解释一段由模型写出的编排脚本，而不是作为一个新原语。

## 分布式

因为事实基础是"在一个持久化日志上 fold"，分布式主要是一个调度问题：任何能读到存储的进程都能通过 fold 重建任何 Task，而执行不对自己身处哪台机器做任何假设。默认形态是单主机 —— 一个本地 SQLite 文件加一个进程内的常驻 `WorkerLoop` 池。触及一个多主机集群是一次存储适配器的更换：把部署指向 Postgres，多个 host 进程共享一个数据库，它们的写按上文所述被 fencing。无论哪种方式，Engine 都不变。

取消遵循与 Engine 停止探针相同的协作式设计：取消会标记该 Task；Worker 与 Engine 在下一个安全点停下；级联取消进行中的 Subtask；后台 shell 进程会被注册，并在其会话关闭时被回收。

## 下一步去哪里

- 概念：[事件溯源](../concepts/event-sourcing.md) ·
  [Fold & snapshot](../concepts/fold-and-snapshot.md) ·
  [Engine & 执行](../concepts/engine-execution.md) ·
  [唤醒与恢复](../concepts/wake-resume.md)
- 参考：[SDK](../reference/sdk.md) ·
  [WorkerLoop](../reference/worker-loop.md) ·
  [插件](../reference/plugins.md) ·
  [与 Claude Agent SDK 的比较](../reference/comparison.md)
- 决策记录：[`docs/adr/`](https://github.com/initxy/noeta/tree/main/docs/adr) —— 每个跨模块决策背后的依据。
