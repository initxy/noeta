# 插件 Surface

一个 Surface 就是一个具名的扩展点，而一条贡献恰好指名其中一个。标准 Surface 有十六个。本页是它们的目录：每一个接受什么、向它贡献的东西如何冲突与排序，以及哪个内置插件演示了它。

loader 是**与 Surface 无关的**——它只咨询一个 `SurfaceRegistry`，别无其他——因此新增一个 Surface 意味着注册一个 `SurfaceSpec`，而绝不是去改 loader。源码：`packages/noeta-sdk/noeta/client/surfaces.py`（`STANDARD_SURFACES`）。

## 怎么读每一节

每一节以 `平面 · 作用范围 · 冲突键 · 排序` 开头。**冲突键**是两条贡献相撞的那个命名空间——`single-valued` 表示整个加载集里最多一条，`none` 表示这个 Surface 从不冲突。**排序**为 `sorted` 时指 `(plugin, name)`，因此发现顺序绝不改变结果；为 `priority` 时先读一个整数 `priority` 参数，并列时再按 `(plugin, name)` 打破平局。

## identity 平面

这些会进入 `AgentSpec` 身份，并且只有在 `Options.plugins` 激活了贡献它的那个插件时才会到达某个 agent。

### `tool`

identity · per-agent · 冲突 `name` · sorted。一个内置工具名，或一个暴露 `.ref` 的对象——一个被 `@tool` 装饰的函数，或一个 Tool 类。内置语料：`fs` 声明八个（`Read`、`Glob`、`Grep`、`Edit`、`Write`、`Bash`、`BashOutput`、`KillShell`），`web` 两个，`memory` 四个。

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

identity · per-agent · 冲突 `name` · **priority**。一个面向模型的 schema，它翻译成一个 engine 决策，而不是一次 `Tool.invoke`。`ref` 是一个 `(ControlToolBuildContext) -> ControlToolMount | None` 工厂，它**自我门控**，不适用时返回 `None`——挂载*本身*就是启用。内置语料，按 schema 渲染顺序（由字节相等的 golden 锁定，因为这个顺序会喂进稳定前缀的哈希）：`Task`（100，`delegation`）、`TodoWrite`（200）、`AskUserQuestion`（300）、`run_workflow`（500）和 `structured_output`（600，后两者都来自 `react`）。

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

wiring · host-wired · 冲突 **single-valued** · sorted。一个 `LLMProvider` 适配器；整个加载集里最多一个。**由宿主自行解析的清单项**——声明出来是为了可审计，由宿主手工解析并接线，绝不自动消费：宿主把自己挑中的适配器作为 `Client(provider=...)` 或 `Options.provider` 传进去，这也正是为什么这里的一条贡献不可能悄悄把它替换掉。与 `sandbox_provider` 是同一套做法（见那一节，以及 `tests/test_extension_surfaces.py`）。官方适配器不在这里声明——它们住在 `providers` 内置项里，经由 `noeta.sdk.providers` 获取。

```toml
[[tool.noeta.contributions]]
surface = "provider"
ref     = "house_style.provider:GatewayProvider"
```

### `reminder_provider`

wiring · per-agent · 冲突 `name` · sorted。轨道 A：一个位于具名摄入接缝（`turn_intake`、`task_seed`）上的 provider，它读取一个很窄的 `RecallView`，返回零个或多个 `Reminder`（作为后续消息落账）和/或 `ResidentActivation`（经 `Engine.record_content` 作为内容通道的常驻内容落账，紧跟在 goal 之后，默认 activate-once——适合「在一个 task 里只进入一次、压缩后仍在」的内容）。它*可以*查询外部系统，因为它的输出会被**记录**下来——恢复时 fold 会从账本里把这些消息和激活折回来，绝不会重新调用这个 provider。它抛出异常会让这一轮大声失败。内置语料：`memory` 在 `turn_intake` 上贡献了 `memory-recall`——tier-1 正文作为 `memory` 类别的激活，指针作为一条 reminder。

```toml
[[tool.noeta.contributions]]
surface = "reminder_provider"
ref     = "house_style.recall:ticket_reminder_provider"
seams   = ["turn_intake"]
```

### `reminder`

wiring · per-agent · 冲突 `name` · **priority**。轨道 B：一个 `render(view) -> str | None`，它是一个 fold 投影的**纯**函数，渲染在动态后缀的尾端。从不被记录，每次 compose 都重新推导，因此稳定前缀在构造上就不会被它触碰。内置语料：`reminders` 贡献了 `unfinished-todos`（100）、`delegation-nudge`（200）和 `read-suggestion`（300）；`react` 贡献了 `collapsed-context`（350）——指向压缩折叠区间的指针，其 `RecallHistory` 工具可以把那段原文读回来。

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

这些由宿主绑定。它们从不按 agent 生效，也不是 `AgentSpec` 身份的一部分——只有 `mcp_server` 一节里注明的那一个后果例外。

这四个里有两个由 `Client` **自动消费**：`skills` 路径和 `mcp_server` 贡献只要插件被加载就生效，既不需要激活，也不需要宿主写代码。另外两个是**由宿主自行解析的清单项**——见各自小节。

### `mcp_server`

host · host-wired · 冲突 `alias` · sorted。一个**进程内**的 MCP 服务器：值就是 `SdkMcpServer`，与 `Options.mcp_servers` 里放的东西完全一样，所以用 `create_sdk_mcp_server` 来构造。`Client` 在构建时把每个已加载插件的贡献并入生效的 `Options.mcp_servers`；服务器捆绑的那些 `@tool` 函数会像任何已声明的工具一样挂载。host-wired 意味着这里不涉及激活——把插件加载进来，服务器就在这个进程里了——但因为那些工具进入了 agent 的工具集，它们确实会进入编译出的身份，这一点和在 `Options` 上声明一个服务器是一样的。

贡献的**名字就是别名**，它和 `Options.mcp_servers`（其别名是服务器自己的 `name`）共用同一个命名空间。撞车——插件对插件，或者插件对配方——会抛出一个同时指名双方的 `PluginError`。没有覆盖一说。一个不是 `SdkMcpServer` 的值会在构建时以一条指名该插件的消息大声失败。

**远程** MCP 服务器不走这个 Surface。它是每一轮按别名经 `HostConfig.mcp_server_resolver` 寻址的，因为它的 spec 带着 url 和凭据，而这些绝不该放进一份静态 manifest。没有内置项声明 `mcp_server`——`mcp` 内置项是纯声明的。

```toml
[[tool.noeta.contributions]]
surface = "mcp_server"
name    = "tickets"                          # 别名
ref     = "house_style.mcp:TICKETS_SERVER"   # 一个 SdkMcpServer
```

### `skills`

host · host-wired · 冲突 `none` · sorted。一个纯资源 Surface：一个指向 skill 包目录的 `path`。没有 `ref`，因为什么都不会被 import。每个已加载插件的目录都会加入 skill 合并的**最低那一层**，它们彼此之间按 `(plugin, 贡献名)` 排序，因此完整的优先级是

```
内置  <  插件贡献的  <  extra_skill_dirs  <  全局 ~/.agents/skills  <  全局 ~/.noeta/skills  <  工作区 .agents/skills  <  工作区 .noeta/skills
```

默认只挂载两个工作区层。家目录层和借入层都是显式选择加入——`extra_skill_dirs`（如 `~/.claude/skills`）和 `global_agents_skills_dir`（`~/.agents/skills`）走 `HostConfig.plugin_config["skills"]`，`global_skills_dir` 是 host 字段——因为服务端 SDK 不应静默读取运行用户的家目录。运维方的 `skills_dir` override 会钉死工作区作用域的技能集（`.agents/skills` 层不会挂在它下面）；`workspace_skills_trust: "trust-store"` 则让两个工作区层都过插件信任库的门。

所以用户自己工作区里的技能永远会盖住同名的插件技能。这些技能包由与其他各层相同的 `SkillIndexer` 建索引，因此整套 frontmatter 契约——`disable-model-invocation`、`allowed-tools`、`priority`——它们全都白拿。

**路径必须是绝对路径。** 同一份 manifest 可能从 wheel 的 package data、一个裸 `.toml`、或者一个单文件 `.py` 读出来，而这几种来源对"相对路径相对于什么"并无共识；与其让它随安装方式不同而指向不同目录，加载器直接以一个指名该插件的 `PluginError` 拒绝。用模块自身的位置拼出来：`str(Path(__file__).parent / "skills")`。一个磁盘上不存在的路径**不是**错误——它索引成一个空层，因此一个按条件发布的技能包只是什么都不贡献而已。

```toml
[[tool.noeta.contributions]]
surface = "skills"
path    = "/opt/house-style/skills"   # 绝对路径
```

### `sandbox_provider`

host · host-wired · 冲突 `name` · sorted。一个部署可以绑定的容器执行适配器。**由宿主自行解析的清单项**——声明出来是为了可审计，由宿主手工解析并接线，绝不自动消费。声明一条会让它可被发现、可做撞车检查，而且完全不执行任何插件代码；随后宿主挑出这次部署想要的那一个并接上（`pset.get("...").resolve(registry)`）。没有任何东西自动绑定它，因为一个进程只有一个 sandbox 后端，而那到底是哪一个属于部署方的决定，不该由"恰好装了哪个插件"来定。可运行的范例在 `tests/test_extension_surfaces.py`（`test_sandbox_provider_end_to_end_from_plugin_surface_to_reattach`）。内置语料：`sandbox` 声明了两个 AIO Sandbox 适配器，`aio-exec-env`（`AioSandboxExecEnv`）和 `aio-browser`（`AioBrowserBackend`）。

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

Noeta 的十八个内置项就是参考 manifest，每个一个目录，位于 `packages/noeta-sdk/noeta/builtins/<name>/__init__.py`：`app`、`ask_user_question`、`browser`、`delegation`、`fs`、`governance`、`mcp`、`memory`、`presets`、`providers`、`react`、`reminders`、`sandbox`、`skills`、`storage`、`todo_write`、`web`、`workspace`。（插件名一律是 snake_case；上面 `control_tool` 一节里首字母大写的 `TodoWrite` / `AskUserQuestion` 是那些内置项挂载的**模型可见工具**名。）上面每一节都点名了演示它的那些内置项；`mcp`、`providers` 和 `storage` 是纯声明的，贡献数为零。新增一项第一方能力，就是在那里新增一个目录。

## 下一步

- [插件 manifest](plugin-manifest.md) —— 如何声明与加载贡献
- [编写插件](../how-to/write-a-plugin.md) —— 面向任务的指南
- [扩展平面](../architecture/extension-planes.md) —— 这些平面为什么落在现在的位置
- [术语表](glossary.md) —— Surface、Activation、Session pack、Control tool mount
