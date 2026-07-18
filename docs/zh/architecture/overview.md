# 架构概览

自顶向下地走读 Noeta 的架构：各包如何分层堆叠，核心事件溯源决策如何塑造每一层，以及扩展面位于何处。对于"X 是什么"这类问题，本页链接到[概念页面](../concepts/event-sourcing.md)而非重新解释；如需精确的 API 签名，请参阅[参考页面](../reference/sdk.md)。

## 三个包

Noeta 以两个库加一个应用的形式发布，按层级堆叠，越往上越接近产品层：

| 包 | 位置 | 角色 |
| --- | --- | --- |
| `noeta-runtime` | `packages/noeta-runtime` | 纯引擎及运行于其上的框架材料：事件、fold、快照、Worker/Dispatcher、存储适配器、Guard、Observer、ReAct Policy、内置工具、provider 适配器、ContextComposer，以及官方预设代理。不依赖其上方任何层，也不依赖特定厂商。 |
| `noeta-sdk` | `packages/noeta-sdk` | 轻量级进程内客户端门面：`query` / `Client` / `Options` / `@tool` 以及重新导出的扩展接口。不含引擎内部实现，不含 HTTP。 |
| `noeta-agent` | `apps/noeta-agent` | 官方产品：一个多用户代理服务器平台——一个在进程内消费 SDK 的 FastAPI 后端，加上它所服务的 React SPA（`apps/web`）。唯一具有网络暴露面的层；入口为 `python -m noeta.agent`。 |

<p align="center">
  <img src="../../assets/architecture.svg" alt="Noeta 架构 — 三个发行包与模块关系" width="820">
  <br>
  <em>应用在进程内驱动 SDK；SDK 转发到 runtime 的引擎、材料和存储。箭头为调用路径。</em>
</p>

三者都将子包贡献到一个共享的 PEP 420 `noeta.` 命名空间中，因此即使发行包边界发生变化，导入路径也保持不变。依赖方向不靠纪律约束——import-linter 在 CI 中强制执行：runtime 内核不得导入 provider 包，SDK 不得导入应用，应用代码只能导入 `noeta.sdk`（有两个刻意的豁免：`noeta.storage` 用于连接具体后端，`noeta.read_models` 用于只读投影）。用户的公共暴露面仅有 `noeta.sdk`；`noeta-runtime` 作为传递依赖到达，用户从不直接导入。

## 事实基础：状态 = fold(log)

一切其他决策的根源：一个任务的事实基础是其仅追加的 EventLog，任何时刻的状态都通过 fold 该日志计算得出——而从不作为一等副本存储。这一概念及其后果在[事件溯源](../concepts/event-sourcing.md)和[Fold & 快照](../concepts/fold-and-snapshot.md)中有详细说明；本节记录使这一承诺在实践中成立的两个架构级机制。

### 四个状态切片，各有唯一写入者

如果任何东西都可以不经事件就改变状态，那么 fold 的重建结果将不再匹配实际运行的内容。因此，任务状态被切分为四个类型化切片，每个切片恰好有一个写入者：

| 切片 | 唯一写入者 | 内容 |
| --- | --- | --- |
| `RuntimeState` | Engine | 滚动的对话消息流、每轮用量 |
| `TaskState` | Policy——仅通过 Decision 中的状态补丁 | 待办事项、决策记录、已激活的技能 |
| `ContextState` | fold 的压缩/思考处理器 | 上下文计划引用、压缩摘要、剥离的思考内容 |
| `GovernanceState` | fold，从事件累积 | 成本、迭代计数、token 计数、子任务结果 |

最能说明问题的是 `TaskState`：Policy 不能直接赋值给自己的长时记忆。它将 `TaskStatePatch` 附加到返回的 Decision 上；Engine 将其作为事件落地；fold 再将其写回。信封还携带一个 `origin` 标记，记录写入角色（engine、model、tool、observer、system）；Policy 合成的消息在进入流之前会清除其 origin，使其无法冒充其他写入者。

### 跨版本 fold 旧记录

事件载荷和状态切片会演进，但数月前挂起的任务仍必须能在当前代码下 fold。规范渲染层（参见 [Fold & 快照](../concepts/fold-and-snapshot.md)）通过两条对称规则来保证这一点：

- **添加字段不得破坏旧记录。** 新字段追加在其切片末尾，赋予默认值，为空时从字节流中省略——因此旧记录（从未有过该字段）和新代码（将其 fold 为默认值）保持字节相等。
- **移除字段不得导致旧快照崩溃。** 恢复旧快照时，当前版本不再识别的键会被过滤掉，而不是传递给会拒绝它们的构造函数。

一条保证"相同的当前状态 fold 为相同的字节"；另一条容忍"由不同版本写入的过去"。当快照完全早于必需字段时，fold 会丢弃它并从头重放——更慢，但永远不会出错。

## 执行栈

### Engine

Engine 将一个任务推进一步——[compose → decide → dispatch](../concepts/engine-execution.md)——它对 Worker、Dispatcher 或 HTTP 一无所知。其类体控制在 500 行预算内：控制流仅路由 Decision，实际工作——发出信封、运行工具、生成子任务——委托给外围处理器。该预算是由 lint 脚本强制执行的可读性目标，而非硬性限制。

### Worker、Dispatcher、Lease

Dispatcher 负责调度：任务入队、Lease 授予、唤醒事件传递和过期回收。Worker 驱动循环：

1. `dispatcher.lease(…)` 返回一个 `Lease(lease_id, task_id, expires_at, wake_event?)`——对一个任务的独占、通过心跳续约的持有权。
2. Worker 将 EventLog fold 为 `RuntimeState`。
3. 如果设置了 `lease.wake_event`，Worker 调用 `engine.note_woken(…)`，写入一个持久化的 `TaskWoken` 信封。
4. Worker 反复调用 `engine.run_one_step(task, lease_id=…)`，直到任务挂起或终止。
5. Worker 调用 `dispatcher.release(lease_id, next_state=…, wake_on=…)`——或在意外异常时调用 `dispatcher.fail(…)`。

单写入者不变式在此通过机械方式强制执行：EventLog 在每次 `emit(lease_id=…)` 时都会咨询 Dispatcher（作为 `LeaseRegistry`），因此只有活跃 Lease 的持有者才能写入任务流。Observer 在每个信封提交后同步看到它，在写入者线程上但在写入者锁之外，异常会被吞没。

排空循环作为库原语 `noeta.runtime.worker.WorkerLoop` 提供——没有运维 CLI。捆绑的代理在进程内运行一个；嵌入者自行调用 `WorkerLoop(…).run_forever(…)`（参见 [WorkerLoop 参考](../reference/worker-loop.md)）。

### 持久化唤醒：机制

[唤醒与恢复](../concepts/wake-resume.md)阐述了保证——单工作者持久化恰好一次传递。机制如下：

- Dispatcher 通过投影将传入的唤醒事件匹配到挂起的任务，并持久化保存匹配结果。传递在 lease 时通过 `Lease.wake_event` 发生。
- Worker 将唤醒事件传入 `engine.note_woken`，后者在步骤继续之前写入 `TaskWoken(wake_event=…)`。此写入是持久化提交点。
- 匹配**在 lease 之后仍然存活**：仅由消费性的 `release(consumed_wake_event=…)` 清除。Worker 在 lease 和 `TaskWoken` 写入之间崩溃时，唤醒事件保持原位；`requeue_stale()` 将任务返回就绪状态，下一次 lease 重新传递相同的唤醒。
- 消费是幂等的。Worker 的唤醒分支是一个以最新匹配 `TaskWoken` 信封为键的恢复状态机：如果重新传递的 `TaskWoken` 已经落地，则在不发出第二个的情况下进行协调。
- 对没有排队唤醒的挂起任务尝试恢复会报告类型化的 `suspended_without_wake_event`——这是一个诊断信息，意为"等待尚未发生的事情"，而非故障。

该保证的范围限于单主机/单工作者。步骤中途的崩溃（在 `TaskWoken` 之后但在步骤其余信封落地之前）在下一次 lease 时恢复：被中断的尝试在无副作用时被密封并自动重新驱动，否则任务被停放等待人工处理。开放的边缘——多工作者 fencing——以及恢复范围在[已知限制](../operations/limitations.md)中有详细记录。

## 上下文组装

每一步，ContextComposer 按波动性排序的三个段从 fold 后的状态组装模型的 View，保持前缀字节稳定以利于 provider KV-cache 复用；压缩是一个记录的事件而非就地编辑。该设计在[Composer & cache](../concepts/composer-and-cache.md)中有详细说明。这里有一个准确性细节：是否应触发压缩是根据 provider 报告的上一步实际输入 token 数（已 fold 进 `RuntimeState`）来判断的，仅对新追加的消息进行估算——字符计数启发式方法会系统性地低估携带 cache、结构化块或图像的提示。

## Provider 边界

Engine 使用中立的内部协议；厂商适配器在边缘进行转换，将厂商错误 fold 为中立分类（瞬态/上下文溢出/致命），并将仅有线协议的机制（如 cache 断点）排除在账本之外。上述内核不得导入 provider 的规则使这一点成为结构性的。参见 [Provider 中立](../concepts/provider-neutrality.md)。

## SDK 暴露面

`noeta.sdk` 是轻量级客户端：构建一个 `Options`，然后用 `query`（单轮）或 `Client`（多轮）在进程内驱动代理。核心设计是对 `Options` 字段的一次切割：

- **身份字段**决定代理如何思考——系统提示、技能、工具集、能力、自定义 Policy。它们进入记录并在 fold 时逐字复现。
- **连接字段**仅将代理挂载到主机上——provider 实例、工作目录、审批回调、Observer。它们不进入身份，因此交换它们不会扰动记录。

这次切割是强制性的，因为记录必须是可复现的：将两者混合，记录就会因为工作目录改变而无法对齐。

可扩展的是五个显式 seam 加上一个装饰器，全部是通过 `noeta.sdk` 重新导出的 `Options` 字段：

| Seam | 扩展内容 |
| --- | --- |
| `policy` | 将 ReAct 大脑替换为你自己的决策函数（带有 `ref` 以保持身份确定性） |
| `guards` | 效果之前的同步检查（参见 [Guard vs Observer](../concepts/guard-observer.md)） |
| `observers` | 只读事件订阅者——审计、指标 |
| `content_channels` | 注册 `ContentKindSpec` 以将自定义常驻内容放入半稳定段 |
| `mcp_servers` | 进程内 SDK MCP 工具，或到外部 stdio / HTTP MCP 服务器的连接器 |
| `@tool` | 为函数标注名称、版本、风险级别和输入模式，使其成为一等工具 |

保持锁定的是：Engine 主循环、Dispatcher/Worker/Lease 机制（主机配置只能调整并发和 lease 时序），以及 ThreeSegmentComposer——整体替换 composer 不在用户暴露面上，因为稳定前缀可复现性是硬性约束；其唯一开放钩子是内容通道。存储后端通过 `HostConfig` 而非 `Options` 连接，从不进入代理身份。

默认值遵循与 Claude Agent SDK 参数表相同的模式：代理获得完整的内置工具集（11 个工具），除非被 `allowed_tools` / `disallowed_tools` 收窄；`permission_mode`（`default` / `acceptEdits` / `bypassPermissions`）决定高风险工具是否先询问。精确签名位于 [SDK 参考](../reference/sdk.md)中。

## 代理层

代理的身份是一个 `AgentSpec`——名称加上身份侧配置（提示、工具、能力）——从 `Options` 编译并收集在注册表中。身份层位于 runtime 较低位置，仅依赖协议层。

四个官方预设随附刻意收窄的暴露面：

| 预设 | 角色 | 工具暴露面 | 可委派？ |
| --- | --- | --- | --- |
| `main` | 对话控制器 | 完整内置工具 + 元能力（todo / 询问用户 / 委派 / 技能 / 记忆 / MCP） | 是 |
| `general-purpose` | 自包含的编码工作者 | 读/写/编辑 + shell + web | 否——叶子节点 |
| `explore` | 只读侦察兵 | 仅只读工具 | 否 |
| `plan` | 只读规划者 | 仅只读工具 | 否——产出计划 |

收窄的工具是 `Capabilities`：显式开关（todo、ask-user、delegate、技能调用、记忆、MCP，加上可生成代理的允许列表）写入代理身份——而非附加的运行时限制。

协作有两种形式。**单次委派**：父代生成一个 Subtask，挂起，并在其完成时唤醒。**扇出**：父代生成一组并发运行在有界进程内线程池上的 Subtask，结果一起回流——每个结果通过唤醒事件返回并配对到原始工具调用。每个 Subtask 都是一个完整的事件溯源任务，有自己的日志和 fold，仅通过 `parent_task_id` 与其父代关联；更复杂的编排表达为一个任务，其 Policy 解释模型编写的编排脚本，而非作为新原语。

## 分布式

一旦事实基础收敛于"在持久化日志上 fold"，分布式主要是一个调度问题。任何能读取存储的进程都可以通过 fold 重建任何任务；执行不假设它在哪台机器上运行。Lease 是防止并发 Worker 操作同一任务的机制：lease、心跳、过期扫描、恰好一次唤醒传递，以及日志本身中的写入验证。

当前的发布形态是单主机：一个本地 SQLite 文件、一个进程内 `WorkerLoop`，以及作为有界进程内线程的扇出（默认 8）。实现多主机集群只需更换存储适配器加上工作者池——Engine 不变，因为 fold 是纯函数且 lease 验证存在于日志中。这项工作是真实的但尚未发布；诚实的边界列表在[已知限制](../operations/limitations.md)中。

取消遵循与 Engine 停止探测相同的协作设计：取消标记任务；Worker 和 Engine 在下一个安全点停止；级联取消进行中的 Subtask；后台 shell 进程被注册并在其会话关闭时回收。

## 下一步去哪里

- 概念：[事件溯源](../concepts/event-sourcing.md) · [Fold & 快照](../concepts/fold-and-snapshot.md) · [Engine & 执行](../concepts/engine-execution.md) · [唤醒与恢复](../concepts/wake-resume.md)
- 参考：[SDK](../reference/sdk.md) · [WorkerLoop](../reference/worker-loop.md) · [与 Claude Agent SDK 的比较](../reference/comparison.md)
- 决策记录：[`docs/adr/`](https://github.com/initxy/noeta/tree/main/docs/adr)——为什么每个跨模块决策是现在这样。
