# 插件扩展点

扩展点（surface）是插件能往里挂东西的具名位置，每个贡献只对应一个。标准扩展点共十六个（`packages/noeta-sdk/noeta/client/surfaces.py` 里的 `STANDARD_SURFACES`）。

## 总览

| 扩展点 | 类别 | 作用范围 | 冲突键 | 排序 | 值 |
| --- | --- | --- | --- | --- | --- |
| [`tool`](#tool) | identity | per-agent | `name` | sorted | 内置工具名、`@tool` 函数或 Tool 类 |
| [`agent`](#agent) | identity | per-agent | `name` | sorted | `AgentDefinition` |
| [`content_kind`](#content-kind) | identity | per-agent | `kind` | sorted | `ContentKindSpec` |
| [`prompt_fragment`](#prompt-fragment) | identity | per-agent | `name` | sorted | 字符串（`text` 或 `ref`） |
| [`policy`](#policy) | identity | per-agent | single-valued | sorted | 带 `.ref` 的 `(llm) -> Policy` 工厂 |
| [`control_tool`](#control-tool) | identity | per-agent | `name` | priority | `(ControlToolBuildContext) -> ControlToolMount \| None` |
| [`guard`](#guard) | wiring | process | none | sorted | 执行前检查 |
| [`observer`](#observer) | wiring | process | none | sorted | `Callable[[EventEnvelope], None]` |
| [`provider`](#provider) | wiring | host-wired | single-valued | sorted | `LLMProvider` |
| [`reminder_provider`](#reminder-provider) | wiring | per-agent | `name` | sorted | 在消息入口处调用的函数 |
| [`reminder`](#reminder) | wiring | per-agent | `name` | priority | `render(view) -> str \| None` |
| [`tool_result_transform`](#tool-result-transform) | wiring | per-agent | `name` | priority | 函数 |
| [`session_pack`](#session-pack) | wiring | per-agent | `name` | priority | `(SessionBuildContext) -> PackContribution` |
| [`mcp_server`](#mcp-server) | host | host-wired | `alias` | sorted | `SdkMcpServer` |
| [`skills`](#skills) | host | host-wired | none | sorted | 绝对目录路径 `path` |
| [`sandbox_provider`](#sandbox-provider) | host | host-wired | `name` | sorted | 沙箱适配器 |

- **类别。** *identity* 类会进入 `AgentSpec` 身份，只有 `Options.plugins` 启用了该插件的 agent 才拿得到。*wiring* 类改变行为，不影响身份。*host* 类由宿主绑定，不按 agent 区分。
- **冲突键。** 两个贡献在哪个范围内会撞车。`single-valued` = 整个加载集合里最多一个；`none` = 永不冲突。
- **排序。** `sorted` = 按 `(plugin, name)`。`priority` = 先按整数参数 `priority`（默认 `0`），相同再按 `(plugin, name)`。

## identity 类

### `tool`

启用这个插件的 agent 会拿到的工具。内置：`fs`（八个）、`web`（两个）、`memory`（四个）。

```toml
[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

### `agent`

启用它的 agent 可以派活的子 agent。内置：`presets` 贡献 `web` 和 `__consolidation__`。

```toml
[[tool.noeta.contributions]]
surface = "agent"
ref     = "house_style.agents:REVIEWER"
```

### `content_kind`

提示词里半稳定部分的一类常驻内容；注册顺序就是排版顺序。内置插件都不用它——内置的几类内容（`skill`、`memory`、`instructions`、`environment`）由 `session_pack` 贡献提供。

```toml
[[tool.noeta.contributions]]
surface = "content_kind"
ref     = "house_style.content:RUNBOOK_KIND"
```

### `prompt_fragment`

追加在系统提示词后面的一段文字。内置：`memory` 贡献 `memory-policy`。

```toml
[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."
```

### `policy`

决策循环。整个加载集合里最多一个——`Options.policy` 加一个插件，或两个插件，都会报错。默认是内置 `react` 的 `("react", "1")`。

```toml
[[tool.noeta.contributions]]
surface = "policy"
ref     = "house_style.policy:build_fsm_policy"
```

### `control_tool`

交给模型的 schema，调用后变成引擎决策而不是 `Tool.invoke`。不适用时工厂返回 `None`。内置按优先级：`Task`（100，`delegation`）、`TodoWrite`（200，`todo_write`）、`AskUserQuestion`（300，`ask_user_question`）、`run_workflow`（500）、`RecallHistory`（550）、`structured_output`（600），后三个来自 `react`。顺序由 golden 测试锁定，因为它影响提示词前缀缓存。

```toml
[[tool.noeta.contributions]]
surface  = "control_tool"
ref      = "house_style.control:build_escalate_control_tool"
priority = 700
```

## wiring 类

### `guard`

在 `before_tool_call`、`before_spawn_subtask` 或 `before_finish` 时同步检查，返回 `allow` / `deny` / `require_approval`。对整个进程生效：加载即对所有 agent 起作用。内置：`governance` 贡献 `permission`、`budget`、`repetition`、`hook`。

```toml
[[tool.noeta.contributions]]
surface = "guard"
ref     = "house_style.guards:NoProdWritesGuard"
```

### `observer`

事件写入日志后收到通知。它影响不了任务，也不能改任何东西。内置：`governance` 贡献 `hook`。

```toml
[[tool.noeta.contributions]]
surface = "observer"
ref     = "house_style.observers:ship_to_siem"
```

### `provider`

一个 `LLMProvider` 适配器，最多一个。只用于登记备查，不会自动接上；宿主自己把选好的适配器传给 `Client(provider=...)` 或 `Options.provider`。官方适配器在 `noeta.sdk.providers`，不走这里。

```toml
[[tool.noeta.contributions]]
surface = "provider"
ref     = "house_style.provider:GatewayProvider"
```

### `reminder_provider`

在指定的入口（`turn_intake`、`task_seed`）被调用，拿到一个 `RecallView`（新消息、折叠后的任务状态、工作区路径、`visible_history`）。返回 `Reminder`（记录为追加消息）和/或 `ResidentActivation`（记录为常驻内容，默认只激活一次）。输出会被记录下来，所以可以查外部系统；恢复时不会重跑。抛异常会让这一轮失败。内置：`memory` 在 `turn_intake` 上贡献 `memory-recall` 和 `memory-index-delta`。记忆索引在一个任务里只记录一次、之后不再更新，所以后者会在下一轮补一行说明：从这个任务开始到现在，新建了哪些页、哪些页的描述改了、哪些页没了。

```toml
[[tool.noeta.contributions]]
surface = "reminder_provider"
ref     = "house_style.recall:ticket_reminder_provider"
seams   = ["turn_intake"]
```

### `reminder`

基于折叠状态的纯函数 `render(view) -> str | None`，每次组装提示词时渲染在末尾，从不记录。内置：`reminders` 贡献 `unfinished-todos`（100）和 `read-suggestion`（300）；`react` 贡献 `collapsed-context`（350）。

```toml
[[tool.noeta.contributions]]
surface  = "reminder"
ref      = "house_style.reminders:stay_brief"
priority = 500
```

### `tool_result_transform`

在工具结果写入日志前改写它（脱敏、截断、加注释）。内置插件都不用。

```toml
[[tool.noeta.contributions]]
surface  = "tool_result_transform"
ref      = "house_style.transforms:redact"
priority = 100
```

### `session_pack`

为每个任务组装某项能力需要的东西（工具、内容类型、命名导出）。不适用时返回空。内置优先级（由 golden 测试锁定）：`fs` 100、`web` 200、`memory` 300、`instructions` 400、`environment` 500（这两个属于 `workspace`）、`skills` 600、`browser` 700、`app` 1000。

```toml
[[tool.noeta.contributions]]
surface  = "session_pack"
ref      = "house_style.pack:build_runbook_session_pack"
priority = 1100
```

## host 类

`mcp_server` 和 `skills` 插件一加载就生效。`provider` 和 `sandbox_provider` 只是登记，宿主自己去接。

### `mcp_server`

用 `create_sdk_mcp_server` 建的进程内 MCP server。`Client` 会把所有加载到的并进 `Options.mcp_servers`；它的工具加入 agent 的工具集（所以也影响身份）。贡献名就是别名，和 `Options.mcp_servers` 共用一个命名空间，撞名报 `PluginError`。远程 server 不在这里声明，而是每轮通过 `HostConfig.mcp_server_resolver` 解析。

```toml
[[tool.noeta.contributions]]
surface = "mcp_server"
name    = "tickets"                          # the alias
ref     = "house_style.mcp:TICKETS_SERVER"   # an SdkMcpServer
```

### `skills`

一个技能包目录，没有 `ref`。路径**必须是绝对路径**（用 `Path(__file__).parent` 拼）；目录不存在就当空的。插件目录的优先级只高于内置：

```
built-in < plugin < extra_skill_dirs < ~/.agents/skills < ~/.noeta/skills < workspace .agents/skills < workspace .noeta/skills
```

| `plugin_config["skills"]` 键 | 含义 |
| --- | --- |
| `extra_skill_dirs` | 额外目录，比如 `~/.claude/skills`（需显式打开） |
| `global_agents_skills_dir` | `~/.agents/skills` 这一层（需显式打开） |
| `skills_dir` | 替换工作区技能目录（此时 `.agents/skills` 不再挂载） |
| `workspace_skills_trust` | `"trust-store"` 让两层工作区技能都要先查信任记录 |
| `menu_budget_tokens` | `skill` 菜单的总长度上限；默认是模型上下文窗口的 1%，最多 4,096 token |
| `menu_rank` | `技能名 → 分数`，菜单超长时按它决定保留顺序 |
| `allow_skill_scripts` | 挂上 `run_skill_script` |

`global_skills_dir` 是宿主字段。默认只挂工作区两层。菜单超长时，先把每条简介缩成第一句（≤ 24 token），还不够就让排在最后的只留名字；每条简介本身上限 384 token。没有 `menu_rank` 也没有 `HostConfig.skill_menu_rank_resolver` 时，按记录下来的使用次数排序。

```toml
[[tool.noeta.contributions]]
surface = "skills"
path    = "/opt/house-style/skills"   # absolute
```

### `sandbox_provider`

容器执行适配器。只登记、查冲突，不自动绑定；宿主自己挑一个（`pset.get("...").resolve(registry)`）。内置：`sandbox` 声明了 `aio-exec-env`（`AioSandboxExecEnv`）和 `aio-browser`（`AioBrowserBackend`）。

```toml
[[tool.noeta.contributions]]
surface = "sandbox_provider"
ref     = "house_style.sandbox:K8sSandboxProvider"
```

## 注册自己的扩展点

```python
from noeta.sdk import SurfaceSpec, load_plugins, standard_registry

reg = standard_registry()                     # a fresh copy each call
reg.register(SurfaceSpec("http_route", "host", "host-wired", _valid_route, "name"))
plugins = load_plugins(registry=reg)
```

| `SurfaceSpec` 字段 | 取值 |
| --- | --- |
| `name` | 清单里写的扩展点名 |
| `plane` | `identity` / `wiring` / `host` |
| `activation_scope` | `per-agent` / `process` / `host-wired` |
| `validator` | 对解析后的值调用；列出和合并时不调用 |
| `collision_key` | `name` / `kind` / `alias` / `single-valued` / `none` |
| `ordering` | `sorted`（默认）/ `priority` |
| `activation_binding` | 仅 identity 类，且必填：`tool` / `agent` / `content_kind` / `prompt_fragment` / `policy` / `elsewhere` |

取值不合法会在构造时抛 `PluginError`。`SurfaceRegistry` 的方法：`register(spec)`（重名报错）、`get(name)`、`names()`、`__contains__`、`copy()`。

## 内置插件

共十八个，每个一个目录，在 `packages/noeta-sdk/noeta/builtins/` 下：`app`、`ask_user_question`、`browser`、`delegation`、`fs`、`governance`、`mcp`、`memory`、`presets`、`providers`、`react`、`reminders`、`sandbox`、`skills`、`storage`、`todo_write`、`web`、`workspace`。其中 `mcp`、`providers`、`storage` 不声明任何贡献。

## 下一步

- [插件清单](plugin-manifest.md)——声明与加载
- [写一个插件](../guides/plugins.md)——上手指南
- [插件系统](../how-it-works/plugin-system.md)——各类扩展点为什么这么分
