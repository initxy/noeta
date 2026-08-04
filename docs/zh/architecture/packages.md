# 包与导入规则

Noeta 以两个共享同一个导入命名空间的库发布。本页讲这条切分是怎么划的、为什么划在这里，以及让它保持诚实的那些导入规则 —— 包括那条让 provider 中立成为结构事实而不是一句承诺的规则。

*使用* SDK 时你完全不需要这些。你需要它们，是在你扩展 Noeta、打包它，或审计内核能够到什么的时候。

<p align="center">
  <img src="../../assets/diagrams/architecture.svg" alt="Noeta 架构 —— noeta-sdk 位于 noeta-runtime 之上，builtins 只能经由插件加载器够到内核" width="820">
  <br>
  <em>host 在进程内驱动 SDK；SDK 转发进 runtime 的引擎、物料与存储。built-in 只能经由加载器够到内核。</em>
</p>

## 两个库

| 包 | 角色 | 依赖 |
| --- | --- | --- |
| `noeta-runtime` | 纯内核 —— 在进程内托管一个 agent 所需的一切，且**自己不带任何能力实现**。 | 仅标准库 |
| `noeta-sdk` | 薄客户端，也是用户唯一导入的东西 —— 外加 `noeta.builtins`，每个官方能力真正落脚的地方。 | `noeta-runtime`、`httpx`、`psycopg` |

runtime 的顶层模块是：`protocols`（带类型的边界，不导入项目内任何其他东西）、`core`（Engine、fold、snapshot）、内核服务 `runtime` / `storage` / `observers` / `read_models`、物料层 `context` / `policies` / `tools`、仅供注入的 `execution` builder、`agent` 身份层，以及 `testing`。

SDK 又加了四个：`client`（组装边界）、`sdk`（公共门面）、`presets`（官方 agent）和 `builtins`（能力目录）。

**用户安装 `noeta-sdk`，并且只导入 `noeta.sdk`。** `noeta-runtime` 作为一个他们从不触碰的传递依赖到达。在 PyPI 上，裸的 `noeta` 名字属于一个无关项目，这也是两个发行包都带后缀的原因。

## 一个命名空间，两个 wheel

两个包都向一个共享的 **PEP 420 命名空间包** `noeta` 贡献子包。两个 wheel 里都没有 `noeta/__init__.py`；Python 在导入时把两棵树合并起来。

这个后果值得直说：无论由哪个 wheel 发布，`noeta.core`、`noeta.context`、`noeta.builtins` 都是稳定的导入路径。把一个模块挪过发行包边界，改变的是*打包*，而不是任何 import 语句 —— 下面每一条契约也都原样重新生效。

## 导入规则

依赖方向不靠自觉。`.importlinter` 在 CI 里运行，一有违规就让构建失败。十条契约撑起这套拓扑；下面是承重的那几条。

**分层栈**（`layers`），自上而下：

```
noeta.sdk
noeta.builtins | noeta.presets
noeta.client
noeta.execution
noeta.context | noeta.policies | noeta.tools
noeta.runtime | noeta.storage | noeta.observers | noeta.read_models
noeta.agent.registry
noeta.agent.spec
noeta.core
noeta.protocols
```

一个模块可以向下导入，绝不可以向上。

**`noeta.protocols` 不导入项目内任何东西**（`protocols-isolation`）。它是其他每一层所讲的那个带类型的边界，所以它不能依赖它们中的任何一个。

**`noeta.core` 只可以导入 `noeta.protocols`**（`core-uses-only-protocols`），带一个有据可查的例外：当调用方没有接上 `ToolRuntime` 时，Engine 会惰性导入 `noeta.runtime.tool.ToolRuntime`，而这条契约按名字把这一条边加进白名单，而不是把整层打开。

**没有任何东西静态导入 `noeta.builtins`**（`sdk-core-not-builtins`）。这是那条通用的微内核契约，下一节讲它买来了什么。

更小的契约让叶子保持狭窄：`noeta.observers` 和 `noeta.read_models` 只看得到 protocols（加上一条被列入白名单的 `core.fold` 边），`noeta.agent.spec` / `registry` 只看得到 protocols，内核词汇沉淀模块 `noeta.runtime.governance` / `mcp` 只看得到 protocols，而生产代码对话的是存储 Protocol，而不是内存版适配器。

## 内核不携带任何能力

每一个官方能力 —— fs 与 web 工具包、provider 适配器、默认 Guard、memory、browser、MCP、sandbox 后端、skills、ReAct Policy、持久化存储后端 —— 都是 `packages/noeta-sdk/noeta/builtins/<name>/` 下的一个 **built-in plugin**。

没有任何东西静态导入那一层。唯一的入口是插件加载器的动态 `ref` 解析，在客户端构建时解析一次。因此导入 `noeta.builtins` 一个实现模块都不会导入，而 `.importlinter` 会拒绝任何会改变这一点的静态边。

由此得出两件事，而且两件都是结构性的，不是口号：

- **Provider 中立。** 每个厂商适配器都住在 `providers` 这个 built-in 里。内核*不可能*导入其中任何一个，所以它也长不出厂商假设。见 [Provider 中立](../concepts/provider-neutrality.md)。
- **第一方能力没有特权。** Noeta 自己的 built-in 走的是与第三方插件完全相同的加载、校验和合并路径，所以这条扩展路径被每个默认 agent 在每次运行中反复检验。

### 目录

`noeta-sdk` 里发布了十八个 built-in：

| 分组 | built-in | 填充 |
| --- | --- | --- |
| 工具包 | `fs`、`web`、`memory`、`browser`、`app`、`workspace` | `tool`、`session_pack`、`prompt_fragment`、`reminder_provider` |
| 控制工具 | `TodoWrite`、`AskUserQuestion`、`delegation`、`react` | `control_tool` |
| 上下文 | `skills`、`reminders` | `session_pack`、`reminder` |
| 治理 | `governance` | `guard`、`observer` |
| agent | `presets` | `agent` |
| 仅声明 | `providers`、`mcp`、`storage`、`sandbox` | （host 接线） |

最后一行才是有意思的那一行。`providers`、`mcp` 和 `storage` 携带**零条贡献**：一个 LLM 适配器、一个 MCP 连接器和一个存储后端全都属于 host 接线，而不是 agent 身份，所以它们是经由 `noeta.sdk.providers` / `noeta.sdk.storage` 和加载器的动态入口触达的，而不是被合并到某个 Surface 上。它们仍然住在这个目录里，因为实现代码本来就该待在那儿。（`sandbox` 在 host 平面上贡献了一条 `sandbox_provider`。）

`react` 拒绝被禁用：它提供的是每个已编译 agent 的身份都钉住的默认决策 Policy，所以这个大脑是通过 `policy` 这个 Surface *可替换的*，而不是可移除的。

## 分布式

因为事实基础是对一份持久日志的 fold，分布式基本上就是一个调度问题：任何能读到存储的进程都能重建任何 Task，而执行不对它跑在哪台机器上作任何假设。

默认形态是单主机 —— 一个本地 SQLite 文件加一个进程内的常驻 worker 池。走向多主机只是换一个存储适配器：把部署指向 Postgres，多个 host 进程共享一个数据库，它们的写入在事务内被加了围栏。两种情况下 Engine 都不变。

两个 wheel 都不讲 HTTP，也都不发布守护进程。想要 API 的 host 会在 `noeta.sdk` 之上自己搭一个；`examples/reference-host` 就是最小的那种 host。

## 接下来去哪

- [状态与写入者](state-and-writers.md) —— 单写入者不变式究竟保护了什么
- [扩展平面](extension-planes.md) —— 加载器所填充的十六个 Surface
- [架构概览](overview.md) —— 自顶向下的导览
- [插件参考](../reference/plugins.md) —— manifest 格式与加载器的各个来源
