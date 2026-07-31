# 插件 Surface

一个 Surface 就是一个具名的扩展点，而一条贡献恰好指名其中一个。标准 Surface 有十六个。本页是它们的目录：每一个接受什么、向它贡献的东西如何冲突与排序，以及哪个内置插件演示了它。

loader 是**与 Surface 无关的**——它只咨询一个 `SurfaceRegistry`，别无其他——因此新增一个 Surface 意味着注册一个 `SurfaceSpec`，而绝不是去改 loader。源码：`packages/noeta-sdk/noeta/client/surfaces.py`（`STANDARD_SURFACES`）。

## 怎么读每一节

每一节以 `平面 · 作用范围 · 冲突键 · 排序` 开头。**冲突键**是两条贡献相撞的那个命名空间——`single-valued` 表示整个加载集里最多一条，`none` 表示这个 Surface 从不冲突。**排序**为 `sorted` 时指 `(plugin, name)`，因此发现顺序绝不改变结果；为 `priority` 时先读一个整数 `priority` 参数，并列时再按 `(plugin, name)` 打破平局。

## identity 平面

这些会进入 `AgentSpec` 身份，并且只有在 `Options.plugins` 激活了贡献它的那个插件时才会到达某个 agent。

### `tool`

identity · per-agent · 冲突 `name` · sorted。一个内置工具名，或一个暴露 `.ref` 的对象——一个被 `@tool` 装饰的函数，或一个 Tool 类。内置语料：`fs` 声明九个（`read`、`glob`、`grep`、`edit`、`write`、`apply_patch`、`shell_run`、`shell_poll`、`shell_kill`），`web` 两个，`memory` 四个。

```toml
[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

### `agent`

identity · per-agent · 冲突 `name` · sorted。激活它的 agent 可以派生的一个子 agent；`ref` 必须解析为一个 `AgentDefinition`。内置语料：`presets` 贡献了 `web` 浏览专家和内部的 `__consolidation__` 记忆策展员。

```toml
[[tool.noeta.contributions]]
surface = "agent"
ref     = "house_style.agents:REVIEWER"
```

### `content_kind`

identity · per-agent · 冲突 `kind` · sorted。半稳定段的一种常驻内容 kind；`ref` 必须解析为一个 `ContentKindSpec`，而注册顺序*就是*布局顺序。没有内置项在这里声明——四个内置 kind（`skill`、`memory`、`instructions`、`environment`）是通过各自的 session pack 进来的。

```toml
[[tool.noeta.contributions]]
surface = "content_kind"
ref     = "house_style.content:RUNBOOK_KIND"
```

### `prompt_fragment`

identity · per-agent · 冲突 `name` · sorted。一个追加在系统 prompt 之后的字面字符串——用 `text` 内联声明，或让 `ref` 指向一个模块级字符串。内置语料：`memory` 贡献了 `memory-policy`，也就是告诉模型该存什么、不该存什么的那段片段。

```toml
[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."
```

### `policy`

identity · per-agent · 冲突 **single-valued** · sorted。决策大脑：一个 `(llm) -> Policy` 工厂，携带一个 `.ref`，其身份会被每个编译出的 `AgentSpec` 钉住。整个加载集里最多一个——一个基础 `Options.policy` 再加上一个活跃插件，或者两个插件，都是错误。默认值是来自 `react` 内置项的 `("react", "1")`：在这里可以被替换，但永远不能被移除。

```toml
[[tool.noeta.contributions]]
surface = "policy"
ref     = "house_style.policy:build_fsm_policy"
```

### `control_tool`

identity · per-agent · 冲突 `name` · **priority**。一个面向模型的 schema，它翻译成一个 engine 决策，而不是一次 `Tool.invoke`。`ref` 是一个 `(ControlToolBuildContext) -> ControlToolMount | None` 工厂，它**自我门控**，不适用时返回 `None`——挂载*本身*就是启用。内置语料，按 schema 渲染顺序（由字节相等的 golden 锁定，因为这个顺序会喂进稳定前缀的哈希）：`spawn_subagent`（100，`delegation`）、`todo_write`（200）、`ask_user_question`（300）、`run_workflow`（500）和 `structured_output`（600，后两者都来自 `react`）。

```toml
[[tool.noeta.contributions]]
surface  = "control_tool"
ref      = "house_style.control:build_escalate_control_tool"
priority = 700
```

## wiring 平面

是行为，不是身份。`guard` 和 `observer` 是仅有的两个**进程级**通道；除它们之外的进程作用域 Surface 会被拒绝，而不是被悄悄归到这两者之一。

### `guard`

wiring · **process** · 冲突 `none` · sorted。一个在 `before_tool_call`、`before_spawn_subtask` 或 `before_finish` 处的同步动作前检查，返回 `allow` / `deny` / `require_approval`。加载即意味着对这个进程里的每个 agent 都生效——agent 作者不得靠省略一个激活来规避拦截。内置语料：`governance` 贡献了 `permission`、`budget`、`repetition` 和 `hook`。

```toml
[[tool.noeta.contributions]]
surface = "guard"
ref     = "house_style.guards:NoProdWritesGuard"
```

### `observer`

wiring · **process** · 冲突 `none` · sorted。一个订阅到 EventLog 的提交后 `Callable[[EventEnvelope], None]`。它的失败无法影响任务，它也不得改动任何东西。内置语料：`governance` 贡献了 `hook`，即面向用户的工具后置与通知 observer。

```toml
[[tool.noeta.contributions]]
surface = "observer"
ref     = "house_style.observers:ship_to_siem"
```

### `provider`

wiring · host-wired · 冲突 **single-valued** · sorted。一个 `LLMProvider` 适配器；整个加载集里最多一个，并且它会与 `Options.provider` 冲突。官方适配器不在这里声明——它们住在 `providers` 内置项里，经由 `noeta.sdk.providers` 获取。

```toml
[[tool.noeta.contributions]]
surface = "provider"
ref     = "house_style.provider:GatewayProvider"
```

### `reminder_provider`

wiring · per-agent · 冲突 `name` · sorted。轨道 A：一个位于具名摄入接缝（`turn_intake`、`task_seed`）上的 provider，它读取一个很窄的 `RecallView` 并返回零个或多个 `Reminder`。它*可以*查询外部系统，因为它的输出会被**记录**下来——恢复时 fold 会从账本里把这条 reminder 折回来，绝不会重新调用这个 provider。它抛出异常会让这一轮大声失败。内置语料：`memory` 在 `turn_intake` 上贡献了 `memory-recall`。

```toml
[[tool.noeta.contributions]]
surface = "reminder_provider"
ref     = "house_style.recall:ticket_reminder_provider"
seams   = ["turn_intake"]
```

### `reminder`

wiring · per-agent · 冲突 `name` · **priority**。轨道 B：一个 `render(view) -> str | None`，它是一个 fold 投影的**纯**函数，渲染在动态后缀的尾端。从不被记录，每次 compose 都重新推导，因此稳定前缀在构造上就不会被它触碰。内置语料：`reminders` 贡献了 `unfinished-todos`（100）、`delegation-nudge`（200）和 `read-suggestion`（300）。

```toml
[[tool.noeta.contributions]]
surface  = "reminder"
ref      = "house_style.reminders:stay_brief"
priority = 500
```

### `tool_result_transform`

wiring · per-agent · 冲突 `name` · **priority**。一个 ToolRuntime 阶段，在一个工具结果被记录**之前**改写它——脱敏、截断、加注。没有内置项声明它；它是为有自己数据规则的宿主准备的。

```toml
[[tool.noeta.contributions]]
surface  = "tool_result_transform"
ref      = "house_style.transforms:redact"
priority = 100
```

### `session_pack`

wiring · per-agent · 冲突 `name` · **priority**。一项能力中负责会话构建的那一半：一个 `(SessionBuildContext) -> PackContribution` 工厂，由内核 builder 在一个按 priority 排序的循环里运行。一个 pack 会基于它的上下文**自我门控**——后端缺席、开关关闭、没有配置——并在不适用时返回空贡献，因此内核里不为任何功能留一个 `if`。内置的条带（由字节 golden 锁定，因为工具插入顺序会喂进稳定前缀的哈希）：`fs` 100、`web` 200、`memory` 300、`instructions` 400、`environment` 500（后两者都来自 `workspace`）、`skills` 600、`browser` 700、`app` 1000。

```toml
[[tool.noeta.contributions]]
surface  = "session_pack"
ref      = "house_style.pack:build_runbook_session_pack"
priority = 1100
```

## host 平面

这些由宿主挑选并绑定。它们从不按 agent 生效，也从不是 `AgentSpec` 身份的一部分。

### `mcp_server`

host · host-wired · 冲突 `alias` · sorted。一个可连接的 MCP 服务器 spec，按它的工具所加的那个别名前缀（`mcp__{alias}__{tool}`）作键。没有内置项声明它——`mcp` 内置项是纯声明的。

```toml
[[tool.noeta.contributions]]
surface = "mcp_server"
name    = "tickets"
ref     = "house_style.mcp:TICKETS_SERVER"
```

### `skills`

host · host-wired · 冲突 `none` · sorted。一个纯资源 Surface：一个指向 skill 包目录的 `path`，会被并入 skill 目录。没有 `ref`，因为什么都不会被 import。

```toml
[[tool.noeta.contributions]]
surface = "skills"
path    = "house_style/skills"
```

### `sandbox_provider`

host · host-wired · 冲突 `name` · sorted。一个部署可以绑定的容器执行适配器。内置语料：`sandbox` 声明了两个 AIO Sandbox 适配器，`aio-exec-env`（`AioSandboxExecEnv`）和 `aio-browser`（`AioBrowserBackend`）。

```toml
[[tool.noeta.contributions]]
surface = "sandbox_provider"
ref     = "house_style.sandbox:K8sSandboxProvider"
```

## 注册你自己的 Surface

`SurfaceSpec` 完整描述一个 Surface，而且每个枚举字段都在构造时校验——因此一个拼错的值，或者一个放错位置的位置参数，会在注册那一行就抛出 `PluginError`，而不是拖到投影时。

| 字段 | 取值 |
| --- | --- |
| `name` | manifest 里写的那个 Surface 名 |
| `plane` | `identity` / `wiring` / `host` |
| `activation_scope` | `per-agent` / `process` / `host-wired` |
| `validator` | 运行在一个**已解析**的值上；列举与合并从不调用它 |
| `collision_key` | `name` / `kind` / `alias` / `single-valued` / `none` |
| `ordering` | `sorted`（默认）/ `priority` |
| `activation_binding` | 仅 identity 平面：`tool` / `agent` / `content_kind` / `prompt_fragment` / `policy` / `elsewhere`。在那里**必填**，在别处**被拒绝** |

`activation_binding` 让身份投影保持表驱动：一个 Surface 声明它供给哪条通道，就能在不改 loader 的前提下抵达 `compile_options`。一个没有 binding 的身份 Surface 会在 resolve 与 compile 之间悄无声息地消失，所以构造函数直接拒绝它。

在一份**副本**上注册——`standard_registry()` 每次调用都返回一个全新的——并且要在加载之前完成；随后同一套校验、冲突与排序流水线会原样跑在你的 Surface 上：

```python
reg = standard_registry()
reg.register(SurfaceSpec("http_route", "host", "host-wired", _valid_route, "name"))
plugins = load_plugins(registry=reg)          # the host's surface is live
```

`SurfaceRegistry` 的方法：`register(spec)`（重名会抛异常）、`get(name)`、`names()`、`__contains__`、`copy()`。

## 内置语料

Noeta 的十八个内置项就是参考 manifest，每个一个目录，位于 `packages/noeta-sdk/noeta/builtins/<name>/__init__.py`：`app`、`ask_user_question`、`browser`、`delegation`、`fs`、`governance`、`mcp`、`memory`、`presets`、`providers`、`react`、`reminders`、`sandbox`、`skills`、`storage`、`todo_write`、`web`、`workspace`。上面每一节都点名了演示它的那些内置项；`mcp`、`providers` 和 `storage` 是纯声明的，贡献数为零。新增一项第一方能力，就是在那里新增一个目录。

## 下一步

- [插件 manifest](plugin-manifest.md) —— 如何声明与加载贡献
- [编写插件](../how-to/write-a-plugin.md) —— 面向任务的指南
- [扩展平面](../architecture/extension-planes.md) —— 这些平面为什么落在现在的位置
- [术语表](glossary.md) —— Surface、Activation、Session pack、Control tool mount
