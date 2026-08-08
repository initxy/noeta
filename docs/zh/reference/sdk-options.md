# Options 与宿主接线

`Options` 是一个 agent 的配方：它被告知什么、可以调用哪些工具、可以花多少、可以委派给谁。`HostConfig` 是另一半——*部署*提供的一切，比如持久化存储或一个 sandbox 容器。把它们分开是刻意的：两个宿主跑同一份 `Options`，无论接线如何，都编译出字节级相同的 agent 身份。

源码：`packages/noeta-sdk/noeta/client/options.py` 和 `client/host_config.py`。

## 身份 vs 接线

`Options` 的字段落入两个桶，而这个划分同时决定了两件事：什么会进入被记录的 `AgentSpec`，以及两个 `Options` 比较相等时什么算数。

- **身份**字段被编译进 `AgentSpec`，是事件日志里"这个 agent *曾经是什么*"的一部分。改一个，你就得到了一个不同的 agent。
- **接线**字段是挂载点。`compile_options` 忽略它们，`Options.__eq__` 也排除它们，因此换一个 provider 或一个工作目录绝不会改写身份。

### 身份字段

| 字段 | 类型 / 默认值 | 说明 |
| --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` —— **必填** | 一个逐字的字符串，或一个在编译时解析的具名 preset |
| `name` | `str = "main"` | 与某个 `agents` 键冲突的名字会抛出 `ValueError` |
| `agents` | `Mapping[str, AgentDefinition] = {}` | 一个**扁平**的 dict，从不嵌套 |
| `allowed_tools` | `tuple[str \| ToolLike, ...] \| None = None` | 一个**替换式**白名单：给了 tuple 就意味着*只有*这些工具。`None` = 全部 10 个内置工具；`()` = 没有工具 |
| `disallowed_tools` | `tuple[str, ...] = ()` | 从当前适用的基础列表中减掉；不存在的名字会被忽略 |
| `permission_mode` | `"default"` \| `"acceptEdits"` \| `"bypassPermissions"` | 在编译时校验 |
| `max_turns` | `int \| None` | `budget.max_iterations` 的语法糖；两个都设会抛出 `ValueError` |
| `skills` | `tuple[str, ...] = ()` | 声明式激活的 skill |
| `plugins` | `tuple[str, ...] = DEFAULT_PLUGINS` | 按 agent 的激活——见[下文](#plugin-激活) |
| `budget` | `BudgetSpec \| None` | `None` ⇒ 一个带 `max_subtask_depth=3` 的默认值，即失控递归的护栏 |
| `policy` | 可调用对象 `(llm) -> Policy`，携带一个 `.ref` | `None` ⇒ 内置的 ReAct policy |
| `mcp_servers` | `tuple[SdkMcpServer, ...] = ()` | 进程内服务器；它们的工具进入身份 |

### 接线字段

| 字段 | 类型 / 默认值 | 说明 |
| --- | --- | --- |
| `provider` | `LLMProvider \| None` | LLM 适配器；`Client(provider=…)` 关键字参数优先 |
| `cwd` | `str \| Path \| None` | 工作目录提示 |
| `model` | `str \| None` | 路由提示；被排除在身份与相等性之外 |
| `metadata` | `Mapping[str, str] = {}` | 观测用标签；排除在身份之外 |
| `can_use_tool` | `(tool_name, arguments) -> bool` | 程序化审批；它的裁决被记录为一个普通的审批事件，带 `resolver="can_use_tool"` |
| `output_schema` | `Mapping \| None` | 最终答案的 JSON Schema；同时挂载 `structured_output` control tool |
| `thinking` | `"adaptive"` \| `"disabled"` \| `None` | 非法值会在构造时抛出 `ValueError` |
| `effort` | `"low"` \| `"medium"` \| `"high"` \| `"xhigh"` \| `"max"` \| `None` | 同上 |
| `guards` | `tuple[Guard, ...] = ()` | 动作前拦截 |
| `observers` | `tuple[Observer, ...] = ()` | 提交后的事件订阅者 |
| `content_channels` | `tuple[ContentKindSpec, ...] = ()` | 对宿主开放的唯一 composer 接缝 |

## 权限模式

`permission_mode` 决定一次高风险工具调用如何被批准。

| 模式 | 哪些工具需要审批 |
| --- | --- |
| `"default"` | 声明的 `risk_level` 不是 `low` 的每一个工具 |
| `"acceptEdits"` | 同样的规则，但减去三个编辑类工具 `Edit` / `Write` |
| `"bypassPermissions"` | 一个都不需要——用于受信任的非交互式运行 |

模式只决定被门控的集合。`Guard` 仍然可以拒绝，而 `Options.can_use_tool` 仍然会去裁决那些被门控拦下的调用。

请在运行时读取合法取值，而不要把它们硬编码：

```python
from noeta.sdk import effort_modes, model_capabilities, permission_modes

print(permission_modes())
# → ('default', 'acceptEdits', 'bypassPermissions')   # 信任度递增
print(effort_modes())
# → ('low', 'medium', 'high', 'xhigh', 'max')          # 强度递增
print(model_capabilities(["claude-sonnet-4-6", "gpt-4o-mini"]))
# → {'claude-sonnet-4-6': {'supports_vision': True},
#    'gpt-4o-mini': {'supports_vision': False}}
```

两个模式元组返回的顺序就是选择器应当展示的顺序，而不是字典序。`model_capabilities` 对每个模型只返回一个键 `supports_vision`——与 provider 自身的视觉门控同名——未收录的选择器返回 `True`：适配器会放行未知模型的图片、把裁决权交给 provider，所以门控不能拦下请求本身会接受的东西。只有目录里明确标记 `supports_vision=False` 的行才会拒绝。

## 插件激活

`Options.plugins` 指名*这个 agent* 使用哪些已加载的插件。激活会进入身份：每个被识别的名字都会折进 `AgentSpec.plugins` 元组，而能力门控就是对那个元组做一次成员检查。

`DEFAULT_PLUGINS` 是 `("fs", "web")`。这两个在这个意义上是**身份惰性**的：激活它们不会打开任何能力开关，也不会改变工具集——无论如何，默认的 10 个工具都是从 `fs` 和 `web` 的 manifest 里读出来的。但它们确实会出现在编译后的 `AgentSpec.plugins` 元组里，因此把它们去掉是一次实实在在的身份变更：

```python
compile_options(Options(system_prompt="x"))[0].plugins             # → ('fs', 'web')
compile_options(Options(system_prompt="x", plugins=()))[0].plugins # → ()
```

一个名字必须是下面三者之一，否则编译会大声失败：

- 一个携带身份的**内置能力包**——`memory`、`browser`、`mcp`、`todo_write`、`ask_user_question`、`skill_invocation`、`delegation`；
- 一个**身份惰性**的内置名字，之所以被识别，是为了让打错字仍然失败——`app`、`fs`、`governance`、`presets`、`providers`、`react`、`reminders`、`sandbox`、`skills`、`storage`、`web`、`workspace`；
- 交给 `Client` 的那个 `PluginSet` 里某个 **插件的名字**。

```python
from noeta.sdk import DEFAULT_PLUGINS, Options

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("memory", "todo_write"),
)
# a typo fails the build, naming both the bad name and where it appeared:
#   ValueError: unknown plugin activation 'memry' on Options.plugins — not a
#   built-in activation (app, ask_user_question, …) and not in the loaded
#   plugin set (<none loaded>). Load it before activating, or fix the name.
```

`delegation` 是唯一一个与结构性能力重叠的激活：一个带 `agents` 名册的 agent 会自动推导出它，而显式写出它只可能把它**打开**——扁平的子 agent 正是这样被授予派生权的。完整契约见 [插件](plugins.md)。

## `AgentDefinition`

扁平的子 agent 配方。子 agent 是叶子——`AgentDefinition` 不能嵌套，因此更深的树是在顶层扁平声明，再通过编译出的 `AgentSpec.spawnable` 接起来。

| 字段 | 说明 |
| --- | --- |
| `description` | **必填且非空**——它会被渲染进 `Task` 的 schema，好让模型知道该把活交给谁 |
| `prompt` | 必填 |
| `tools` | `None` ⇒ 全部内置工具 |
| `model` | 路由提示 |
| `plugins` | 按 agent 的激活，默认 `()`——没有 `fs`/`web`；`("delegation",)` 授予派生权 |
| `metadata` | 观测用标签 |

## `SystemPromptPreset`

`preset: str = "main"`、`append: str | None = None`。在编译时解析一个已注册的 preset prompt，可选地追加一个后缀。`register_preset_prompt(name, prompt)` 可以新增一个（后写者获胜）。官方 preset `main` 和 `main-web` 已经替你注册好了——见[预设代理](presets.md)。

## `compile_options` 与 `BudgetSpec`

```python
compile_options(options, *, plugins=None, preset_prompts=None)
    -> (AgentSpec, tuple[AgentSpec, ...])
```

把配方纯粹地编译成 `(main_spec, descendant_specs)`——引用透明，因此相等的 `Options` 产出相等的 `AgentSpec`。`plugins` 是一个 `Mapping[str, PluginActivation]`；`Client` 会从 `PluginSet` 构建它。

`BudgetSpec`（`noeta/agent/spec.py`）承载 `Options.budget` 上的各项上限：`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks`、`max_subtask_depth`。某个字段为 `None` 表示那个维度不设上限。

## `HostConfig`

一个冻结的 dataclass，作为 `Client(..., host_config=…)` 传入。它**从不**是 agent 身份的一部分，因此两个仅在这里不同的 client 编译出字节级相同的 spec。每个字段都默认为"缺省"，因此一个裸的 `HostConfig()` 重现的就是内存存储、无预览、无 MCP 的行为。

**存储。** `storage_triple()` 返回解析出的三元组或 `None`。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `storage_path` | `None` | 一个字符串——一个 sqlite 文件路径、一个 `postgresql://` DSN，或 `":memory:"`——经由 `noeta.sdk.storage.open_storage_stack` 解析 |
| `event_log` / `content_store` / `dispatcher` | `None` | 显式的三元组，要么全给要么全不给 |

两种形式同时提供会抛出 `ValueError`，只给出部分显式三元组也一样。全为 `None` 意味着内存存储。

**运行时注入。**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `app_gateway` | `None` | `AppPreviewGateway`；`None` ⇒ 没有 `open_app` 工具 |
| `write_roots` | `None` | `(task_id) -> Sequence[str]`，额外的写入根 |
| `mcp_server_resolver` | `None` | `(alias) -> McpAnyServerSpec \| None`，按轮解析 |
| `mcp_http_post` | `None` | 为远程 MCP 注入的 HTTP 传输（`HttpPostFn`） |
| `delta_sink` | `None` | `(StepContext, call_id, StreamDelta) -> None` —— 瞬时的 token delta；从不持久化 |
| `otlp_traces` / `otlp_http_post` | `None` | `OtlpTraceConfig` 导出配置以及传输 |
| `provider_headers` | `None` | `(StepContext) -> Mapping[str, str]`，按请求的 header |

**Sandbox 与执行环境。**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `exec_env` | `None` | `SandboxExecEnvConfig` —— **attach** 一个共享容器 |
| `sandbox_provider` | `None` | `SandboxProvider` —— 每个会话新开一个容器；优先于 `exec_env` |
| `sandbox_spec` | `None` | 每会话 `SandboxSpec` 中由部署固定的那一半 |
| `sandbox_exec_preamble` | `None` | `(exec_env_ref, argv) -> prefix`，每条容器命令都会重新调用 |
| `sandbox_backend_factory` / `sandbox_browser_factory` | `None` | 在不触碰接缝的前提下更换 sandbox 的线上实现 |
| `sandbox_policy` | `None` | `(root_task_id, workspace_dir) -> bool`，按会话的选择退出 |

**记忆。** 优先级为 `memory_root_resolver` > `memory_dir` > `global_memory_dir` > `~/.noeta/memories`。见[按租户隔离记忆](../how-to/multi-tenant-memory.md)。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `memory_dir` / `global_memory_dir` | `None` | 宿主级的存储根 |
| `memory_root_resolver` | `None` | `(task_id) -> Path \| None`，按任务的根 |

**插件运维配置。**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `plugin_config` | `{}` | `插件名 -> {键: 值}`，由 session pack 通过 `ctx.config("<name>")` 读取。第三方名字原样透传；对 SDK 自己推导的那四个（`fs` / `skills` / `workspace` / `memory`），host 给的键是**逐键覆盖**的。见[写一个插件](../how-to/write-a-plugin.md) |

**总开关与策略。**

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `workflow_allowed` | `False` | 暴露 `run_workflow`（同时还需要具备委派能力） |
| `max_background_jobs_per_root_task` | `8` | 超过上限时一次后台 `Bash` 会被拒绝，而不是排队 |
| `max_background_subagents_per_root_task` | `8` | 对 `Task(background=True)` 同理 |
| `instructions_enabled` | `False` | 加载工作区根的 `NOETA.md`，否则 `AGENTS.md`，再否则 `CLAUDE.md` |
| `instructions_file` | `None` | 只读这一个路径，不做搜索 |
| `instructions_discovery` | `False` | 由 `Read` 触发的子目录 instructions 文件发现 |
| `write_mode` | `"dry_run"` | `"apply"` 才执行真实写入 |

## Sandbox 与存储的接线类型

| 符号 | 角色 |
| --- | --- |
| `SandboxProvider` | 产品需实现的 `allocate` / `release` / `attach` Protocol |
| `SandboxSpec` / `MountSpec` | `allocate` 的输入：镜像、挂载、资源、环境变量；一个挂载的 `kind` 是 `local-path` / `nas` / `volume` / `pvc` |
| `SandboxHandle` | 一个活的绑定：`base_url`、`sandbox_id`、`auth`、`workdir` |
| `SandboxAuth` / `StaticApiKeyAuth` | `connect_headers()` Protocol 及其环境变量实现；从不被序列化 |
| `encode_exec_env_ref` / `decode_exec_env_ref` | 扁平的持久化 `exec_env_ref` 编解码器 |
| `SandboxExecEnvConfig` | attach 模式的配置：`base_url`、`api_key_env`、`workdir` |
| `ExecEnv` / `BrowserBackend` | 容器执行与浏览器线上协议的 Protocol |
| `BackendFactory` / `BrowserBackendFactory` / `BoundPreamble` | 两个 `HostConfig` 工厂字段所依据的可调用类型别名 |
| `McpServerSpec` / `McpHttpServerSpec` / `McpAnyServerSpec` / `McpError` / `McpConfigError` / `HttpPostFn` | 一个 resolver 返回的 MCP 词汇 |
| `path_within(resolved, root) -> bool` | 写入围栏所用的包含判定——按路径分量比较，绝不是字符串前缀，因此 `/srv/app-old` 不在 `/srv/app` 之内 |

`noeta.sdk.storage` 是通往持久化后端的门。`open_storage_stack(path)` 从一个字符串构建整个 `(event_log, content_store, dispatcher)` 三元组；`build_storage_stack`、`is_memory_path` 和 `is_postgres_url` 是更细粒度的入口，而 sqlite 与 postgres 适配器（连同它们的只读变体和 schema 版本错误）也都从同一个模块导出。

## 下一步

- [query / Client](sdk-client.md) —— 运行这份配方的那些动词
- [插件](plugins.md) —— 一个激活名可以指向什么
- [预设代理](presets.md) —— 四份官方 `Options` 配方
- [内置工具](tools.md) —— `allowed_tools=None` 究竟选中了什么
