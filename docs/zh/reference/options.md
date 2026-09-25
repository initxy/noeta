# Options 与 HostConfig

`Options` 描述 agent 本身；`HostConfig` 描述它跑在什么样的部署里。源码：`packages/noeta-sdk/noeta/client/options.py` 和 `client/host_config.py`。

```python
from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a careful coding agent.",
    permission_mode="acceptEdits",
    max_turns=40,
)
host = HostConfig(storage_path="noeta.sqlite", write_mode="apply")

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5", host_config=host) as client:
    client.start(goal="Fix the failing test in tests/test_utils.py")
```

## `Options`

一个不可变的 dataclass。字段分两类：**身份**字段会编译进记录下来的 `AgentSpec`，改了就算另一个 agent；**接线**字段 `compile_options` 不看，`==` 比较也不算，所以换 provider、换目录都不改变 agent 身份。

### 身份字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `system_prompt` | `str \| SystemPromptPreset` | 必填 | 提示词原文，或一个预设名 |
| `name` | `str` | `"main"` | agent 名字，不能和 `agents` 里的键重名 |
| `skills` | `tuple[str, ...]` | `()` | 给这个 agent 开的技能 |
| `budget` | `BudgetSpec \| None` | `None` | 上限；`None` 等于 `BudgetSpec(max_subtask_depth=3)` |
| `plugins` | `tuple[str, ...]` | `DEFAULT_PLUGINS` = `("fs", "web")` | 这个 agent 启用的插件（见[下文](#启用插件)） |
| `agents` | `Mapping[str, AgentDefinition]` | `{}` | 子 agent，一层平铺的 dict |
| `allowed_tools` | `tuple[str \| tool, ...] \| None` | `None` | 整体替换工具列表；`None` 是 10 个内置工具，`()` 是一个都没有 |
| `disallowed_tools` | `tuple[str, ...]` | `()` | 从当前工具列表里去掉；不存在的名字直接忽略 |
| `permission_mode` | `str` | `"default"` | `default` / `acceptEdits` / `bypassPermissions` |
| `max_turns` | `int \| None` | `None` | `budget.max_iterations` 的简写；两个都设会抛 `ValueError` |
| `policy` | 带 `.ref` 的 `(llm) -> Policy` | `None` | 替换内置的 ReAct 循环 |
| `mcp_servers` | `tuple[SdkMcpServer, ...]` | `()` | 进程内 MCP server，工具会并入工具列表 |

10 个内置工具是 `Read`、`Write`、`Edit`、`Glob`、`Grep`、`Bash`、`BashOutput`、`KillShell`、`WebFetch`、`WebSearch`（`WebSearch` 只在设了 `NOETA_WEB_SEARCH_API_KEY` 时出现）。见[工具](tools.md)。

### 接线字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider` | `LLMProvider \| None` | `None` | 模型适配器；`Client(provider=...)` 优先 |
| `model` | `str \| None` | `None` | 主循环用的模型 id 或别名 |
| `compaction_model` | `str \| None` | `None` | 只用来写上下文压缩摘要的便宜模型；`None` 就用 `model` |
| `recall_model` | `str \| None` | `None` | 关键词没召回到记忆时，用这个模型再判断一次；`None` 只做关键词召回 |
| `webfetch_model` | `str \| None` | `None` | `WebFetch` 读网页时用来总结的模型；`None` 用主模型 |
| `metadata` | `Mapping[str, str]` | `{}` | 给 observer 看的标签 |
| `cwd` | `str \| Path \| None` | `None` | `Client` 没给 `workspace_dir` 时用的工作目录 |
| `can_use_tool` | `(tool_name, arguments) -> bool` | `None` | 用代码批准或拒绝需要审批的调用；记录里 `resolver="can_use_tool"` |
| `output_schema` | `Mapping \| None` | `None` | 最终答案的 JSON Schema；答案会解析成 `dict` / `list`（解析不了就保留原文） |
| `thinking` | `"adaptive" \| "disabled" \| None` | `None` | 推理模式；`None` 用 provider 默认 |
| `effort` | `"low" \| "medium" \| "high" \| "xhigh" \| "max" \| None` | `None` | 推理强度 |
| `guards` | `tuple[Guard, ...]` | `()` | 动作执行前的检查，可以拦下 |
| `observers` | `tuple[Observer, ...]` | `()` | 每个事件提交后的回调 |
| `content_channels` | `tuple[ContentKindSpec, ...]` | `()` | 额外常驻上下文的内容块 |

`thinking` / `effort` 取值不对会在构造时抛 `ValueError`；`thinking="disabled"` 配 `effort="xhigh"` 或 `"max"` 也会抛（Anthropic 不接受这个组合）。

### `AgentDefinition`

一个子 agent。不能嵌套：所有 agent 都在 `Options.agents` 顶层声明。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `description` | `str` | 必填 | 在 `Task` 工具里给父模型看；空白会抛 `ValueError` |
| `prompt` | `str` | 必填 | 子 agent 的提示词 |
| `tools` | `tuple \| None` | `None` | `None` 是内置工具 |
| `model` | `str \| None` | `None` | 子 agent 用的模型（`haiku` 这类别名会按模型目录解析）；`None` 用宿主默认 |
| `plugins` | `tuple[str, ...]` | `()` | 没有 `fs`/`web` 默认值；只有这里写了 `("delegation",)`，子 agent 才有 `Task` 工具 |
| `metadata` | `Mapping[str, str]` | `{}` | 标签，不算身份 |

### `SystemPromptPreset`

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `preset` | `str` | `"main"` | 用 `register_preset_prompt(name, prompt)` 注册过的名字（后注册的覆盖先注册的） |
| `append` | `str \| None` | `None` | 空一行后追加的文字 |

`main` 和 `main-web` 已经注册好了，见 [Presets](presets.md)。

### `BudgetSpec`

字段都默认 `None`（不设上限）：`max_iterations`、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks`、`max_subtask_depth`。计数覆盖任务整个生命周期，不按轮清零。

### `compile_options`

```python
compile_options(options, *, plugins=None, preset_prompts=None)
    -> tuple[AgentSpec, tuple[AgentSpec, ...]]
```

纯函数：相同的 `Options` 编出相同的 spec。`plugins` 是插件名到 `PluginActivation` 的映射（`Client` 会从 `PluginSet` 生成）；`preset_prompts` 用来替换进程级的预设表，编译结果就不受别处注册的影响。

## 审批模式

| 模式 | 执行前要审批的 |
| --- | --- |
| `default` | 所有 `risk_level` 不是 `low` 的工具 |
| `acceptEdits` | 同上，但 `Edit` 和 `Write` 不用 |
| `bypassPermissions` | 都不用 |

`Bash` 和 `WebFetch` 还会按单次调用判断：命令不在 shell 白名单里，或网址不在 `HostConfig.webfetch_allowed_hosts` 里，就要审批（`bypassPermissions` 下除外）。任何模式下 `Guard` 都还能拦。

合法取值在运行时读，顺序就是界面上该显示的顺序：

```python
from noeta.sdk import effort_modes, model_capabilities, permission_modes

permission_modes()   # ('default', 'acceptEdits', 'bypassPermissions')
effort_modes()       # ('low', 'medium', 'high', 'xhigh', 'max')
model_capabilities(["claude-sonnet-4-6", "gpt-4o-mini"])
# {'claude-sonnet-4-6': {'supports_vision': True}, 'gpt-4o-mini': {'supports_vision': True}}
```

目录里没有的模型，`supports_vision` 报 `True`。

## 启用插件

`Options.plugins` 列出这个 agent 用哪些插件，名字会记进 `AgentSpec.plugins`。名字只能是下面几种：

| 类别 | 名字 |
| --- | --- |
| 内置功能（会打开对应能力） | `memory`、`browser`、`mcp`、`todo_write`、`ask_user_question`、`skill_invocation`、`delegation` |
| 内置但不影响 agent（认得这些名字，是为了让拼错能报错） | `app`、`fs`、`governance`、`presets`、`providers`、`react`、`reminders`、`sandbox`、`skills`、`storage`、`web`、`workspace` |
| 已加载的插件 | 传给 `Client` 的 `PluginSet` 里的任意名字 |

```python
from noeta.sdk import DEFAULT_PLUGINS, Options

Options(system_prompt="...", plugins=DEFAULT_PLUGINS + ("memory", "todo_write"))
# plugins=("memry",) 编译时报错：
#   ValueError: unknown plugin activation 'memry' on Options — not a built-in activation (...)
```

`agents` 不为空时会自动加上 `delegation`；手动写它只会打开、不会关掉。去掉 `fs`/`web` 不会少掉默认工具，但记录下来的身份会变。

## `HostConfig`

一个不可变 dataclass，通过 `Client(..., host_config=...)` 传入，永远不算 agent 身份。`HostConfig()` 就是内存存储、不用沙箱、不接 MCP。

### 存储

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `storage_path` | `str \| None` | `None` | sqlite 文件路径、`postgresql://` 连接串，或 `":memory:"`；空字符串报错。由它打开的存储会在 `shutdown()` 时关闭 |
| `event_log`、`content_store`、`dispatcher` | 适配器 | `None` | 直接传存储对象；三个要么都给，要么都不给 |
| `queue` | `str` | `"default"` | 共享存储时这个 client 的 worker 队列；它的 worker 只领这个队列的活 |

`storage_path` 和三个对象同时给，或三个只给了一部分，都会抛 `ValueError`。`noeta.sdk.storage.open_storage_stack(path)` 用一个字符串建出这三个对象；同一模块还导出 `build_storage_stack`、`is_memory_path`、`is_postgres_url` 以及 Sqlite / Postgres 适配器。

### 模型调用与 MCP

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider_headers` | `(StepContext) -> Mapping[str, str]` | `None` | 每次模型请求额外带的请求头（比如网关的粘性标识） |
| `delta_sink` | `(StepContext, call_id, StreamDelta) -> None` | `None` | 流式 provider 的实时 token 增量；不落盘 |
| `extra_models` | `Mapping[str, ModelSpec]` | `{}` | 追加到模型目录的条目；重名会报错；每次运行都要注册同样的条目 |
| `mcp_server_resolver` | `(alias) -> McpAnyServerSpec \| None` | `None` | 每轮把 MCP 别名解析成 server 配置 |
| `mcp_http_post` | `HttpPostFn` | `None` | 远程 MCP 用的自定义 HTTP 传输 |
| `mcp_idle_ttl` | `float \| None` | `1800.0` | 池里没人用的 MCP 连接保留多少秒；`None` 永不关闭；负数报错 |
| `mcp_scope_resolver` | `(task_id) -> str \| None` | `None` | 连接池分区（比如租户 id）；只有同一分区的任务才共用连接 |
| `otlp_traces` | `OtlpTraceConfig` | `None` | OTLP/HTTP 链路导出：`endpoint`、`headers=()`、`service_name="noeta"`。模型调用的 span 带 `latency_ms`、五项用量和 `gen_ai.usage.input_tokens` / `output_tokens`；失败任务的 span 带 `noeta.fail_detail`；子 agent 在另一台机器上导出时，span 仍然挂在父任务下面 |
| `otlp_http_post` | 函数 | `None` | 导出用的自定义传输 |

### 沙箱

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `exec_env` | `SandboxExecEnvConfig` | `None` | 接到一个共用的容器：`base_url`、`api_key_env="SANDBOX_API_KEY"`、`workdir="/workspace"` |
| `sandbox_provider` | `SandboxProvider` | `None` | 每个根任务一个新容器；优先于 `exec_env` |
| `sandbox_spec` | `SandboxSpec` | `None` | 每次分配时固定的部分：`image`、`mounts`、`resources`、`env` |
| `sandbox_exec_preamble` | `(exec_env_ref, argv) -> str` | `None` | 每条命令前现算的 shell 前缀（用来带上新鲜的凭证） |
| `sandbox_backend_factory`、`sandbox_browser_factory` | 工厂函数 | `None` | 替换沙箱或浏览器的客户端实现 |
| `sandbox_policy` | `(root_task_id, workspace_dir) -> bool` | `None` | 返回 `False` 时这个任务在本机跑 |
| `app_gateway` | `AppPreviewGateway` | `None` | 打开 `open_app` 预览工具 |
| `write_roots` | `(task_id) -> Sequence[str]` | `None` | 任务在工作目录之外还能写的目录 |
| `write_mode` | `"dry_run" \| "apply"` | `"dry_run"` | `"apply"` 才会真正写文件；其他值直接报错 |

### 记忆

记忆目录的优先级：`memory_root_resolver` > `memory_dir` > `global_memory_dir` > `~/.noeta/memories`。见[按租户隔离记忆](../guides/multi-tenant-memory.md)。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `memory_dir`、`global_memory_dir` | `Path \| None` | `None` | 宿主级的记忆目录 |
| `memory_root_resolver` | `(task_id) -> Path \| None` | `None` | 按任务决定记忆目录；同一任务每次必须返回同样的结果 |
| `recall_exclude` | `Collection[str]` | `()` | 自动召回永远不带的页面（索引里仍能看到，也能读）；必须是字符串集合，直接给一个 `str` 会报错 |
| `memory_max_bytes` | `int \| None` | `None` | `memory_write` 落盘的整页（字段加正文）超过这么多 UTF-8 字节就拒绝；必须是正数；建议小于 4096，这样召回时能整页带上 |
| `memory_read_only` | `bool` | `False` | 只给 `memory_read` 和 `memory_search`，记忆提示里也去掉怎么写的说明 |
| `memory_index_budget_tokens` | `int \| None` | `None` | 记忆索引的长度上限，召回判断用的索引也受它限制；`None` 是上下文窗口的 1%；必须是正数 |

### 技能与插件

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `skill_menu_rank_resolver` | `(task_id) -> {skill: score} \| None` | `None` | 技能菜单超长时，哪些技能保留完整描述 |
| `skill_usage_ranking` | `bool` | `True` | 没有 resolver 时按整个存储里的近期使用排序；设了记忆或 MCP 分区 resolver 时自动关闭 |
| `plugin_config` | `Mapping[str, Mapping[str, Any]]` | `{}` | 给各插件的运维配置；对 `fs` / `skills` / `workspace` / `memory`，你给的键逐个覆盖 SDK 自己算出来的；Client 不认识的插件名会发 `UserWarning` |

技能菜单占上下文窗口的 1%，最多 4,096 token（宿主推导出的记忆索引预算也用同一个上限）。超了就先把描述缩成一句话，再不够就只留名字，排名低的先缩。静态排序可以直接写在 `plugin_config["skills"]["menu_rank"]`，和 resolver 二选一。

### 限制与开关

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `repetition_threshold` | `int \| None` | `None` | 同样的 `(tool, arguments)` 调用重复这么多次后要审批；必须是正数 |
| `tool_output_inline_limit` | `int \| None` | `None` | 工具结果超过这么多字符就截断再给模型看（完整内容仍有记录）；必须是正数；恢复任务时要保持同一个值 |
| `hooks` | `HooksConfig \| None` | `None` | 用户自定义钩子：工具调用前先过一遍规则，调用结束后、或者等审批时跑一条命令；见[钩子](#钩子) |
| `webfetch_allowed_hosts` | `Sequence[str]` | `()` | `WebFetch` 不用审批就能访问的站点：`"example.com"` 精确匹配，`"*.example.com"` 只匹配子域名；写错会报错 |
| `workflow_allowed` | `bool` | `False` | 提供 `run_workflow`（还需要能派子 agent） |
| `max_background_jobs_per_root_task` | `int` | `8` | 后台 `Bash` 任务超过这个数直接拒绝；必须是正数 |
| `max_background_subagents_per_root_task` | `int` | `8` | `Task(background=True)` 同理；必须是正数 |
| `environment_enabled` | `bool` | `True` | 任务开始时记录工作目录 / git / 平台信息 |
| `instructions_enabled` | `bool` | `False` | 从工作目录根加载 `NOETA.md`，没有就 `AGENTS.md`，再没有就 `CLAUDE.md` |
| `instructions_file` | `Path \| None` | `None` | 只加载这个文件，不再查找；设了它却没开 `instructions_enabled=True` 会报错 |
| `instructions_discovery` | `bool` | `False` | agent 读到子目录时，也加载那里的说明文件 |
| `reliability_sink` | `(ReliabilityEvent) -> None` | `None` | 接收 `start_workers` 起的 worker 池发出的可靠性信号（`stale_requeued`、`heartbeat_invalid_lease`、`step_failed_retryable` 等）；`None` 时写日志。这些不是日志里的事件 |

::: warning
`webfetch_allowed_hosts` 只管要不要弹审批。`WebFetch` 自己不拦任何地址，出网限制要在网络层或沙箱里做。
:::

### 钩子

`HooksConfig` 在构造时就做校验，写了等于没写的规则直接抛 `ValueError`。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `pre_tool_use` | `tuple[PreToolUseRule, ...]` | `()` | 每次调用工具前检查，第一条命中的规则说了算。只能在内置检查放行的基础上再收紧。这些规则属于检查链的一部分，恢复任务的宿主必须传同一套 |
| `post_tool_use` | `tuple[PostToolUseRule, ...]` | `()` | 命中的工具调用结束后跑一条命令 |
| `notification` | `tuple[NotificationRule, ...]` | `()` | 有工具调用开始等审批时跑一条命令 |
| `command_timeout_s` | `float` | `30.0` | 钩子命令跑超过这么多秒就杀掉；必须是正数 |
| `max_queue` | `int` | `256` | 最多排队这么多条命令；再多的直接丢掉并记一条警告，不拖慢 agent；必须是正数 |

| 规则 | 字段 |
| --- | --- |
| `PreToolUseRule` | `match_tool`（按工具名做 `fnmatch` 匹配，比如 `"mcp__*"`）、`action`（`"allow"` / `"deny"` / `"require_approval"`）、`match_arg=None`、`reason=None`（模型或审批人看到的理由） |
| `MatchArg` | `path`（参数键组成的元组，比如 `("opts", "force")`）、`op`（`"equals"` / `"contains"` / `"regex"`）、`value`、`pattern`；`regex` 如果用字符串写在 `value` 里，构造配置时会编译好 |
| `PostToolUseRule` | `match_tool`、`command=None`（argv 元组，不经过 shell）、`log=False`（记一行日志，可以代替命令，也可以和命令一起用） |
| `NotificationRule` | `on`（目前只有 `"approval"`）、`command=None`、`log=False` |

调用后和等审批时跑的命令只是附带动作。每个 Client 起一个后台观察者来跑，工作目录是 Client 的工作目录，环境变量只留 `PATH`、`HOME`、`LANG`、`LC_ALL`、`TERM`、`TMPDIR`。它们不会卡住 agent，不写事件日志，任务恢复时也不会重跑。`shutdown()` 会停掉观察者，并取消还在跑的命令。

```python
from noeta.sdk import HooksConfig, HostConfig, MatchArg, PostToolUseRule, PreToolUseRule

hooks = HooksConfig(
    pre_tool_use=(
        PreToolUseRule(
            match_tool="Bash",
            action="require_approval",
            match_arg=MatchArg(path=("command",), op="regex", value=r"\bgit push\b"),
            reason="推代码需要人确认",
        ),
    ),
    post_tool_use=(PostToolUseRule(match_tool="Edit", command=("make", "fmt")),),
)
client = Client(options, provider=provider, host_config=HostConfig(hooks=hooks))
```

## 接线用到的类型

| 名字 | 说明 |
| --- | --- |
| `SandboxProvider` | Protocol：`allocate` / `release` / `attach` |
| `SandboxSpec`、`MountSpec` | 分配容器的输入；`MountSpec(source, target, mode="rw", kind="local-path")`，`kind` 可选 `local-path` / `nas` / `volume` / `pvc` |
| `SandboxHandle` | 一个在用的容器：`base_url`、`sandbox_id`、`auth`、`workdir="/workspace"` |
| `SandboxAuth`、`StaticApiKeyAuth` | `connect_headers()` Protocol 和读环境变量的实现 |
| `encode_exec_env_ref`、`decode_exec_env_ref` | 记录下来的容器引用的编解码 |
| `ExecEnv`、`BrowserBackend` | 命令执行和浏览器的 Protocol |
| `BackendFactory`、`BrowserBackendFactory`、`BoundPreamble` | 沙箱工厂字段的类型 |
| `McpServerSpec`、`McpHttpServerSpec`、`McpAnyServerSpec` | `mcp_server_resolver` 的返回值（stdio、HTTP、二者之一）；`call_timeout_s`（默认 `None`，即 30 秒）限制每次 `tools/call` 的时长，必须是正数；`deferred=True` 让这个服务器的工具 schema 不随请求发送，模型改用 `ToolSearch` / `McpCall`（见 [MCP server](../guides/mcp.md)） |
| `HttpPostFn`、`McpHttpResponse`、`McpError`、`McpConfigError` | MCP 传输和错误 |
| `OtlpTraceConfig` | 链路导出配置 |
| `path_within(resolved, root) -> bool` | 写入围栏用的路径包含判断，按路径分段比较（`/srv/app-old` 不在 `/srv/app` 里面） |

## 下一步

- [SDK 参考](sdk.md)：运行这份配置的方法
- [Presets](presets.md)：现成的 `Options`
- [部署 worker](../guides/deploy.md)：存储、worker 和 Docker 的实际用法
