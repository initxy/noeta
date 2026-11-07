# Python 接口

能直接从 `noeta` 导入的名字（`noeta/__init__.py`）及其签名，按用途分组。它们在第一次用到时才加载，
所以 `import noeta` 很轻。其余的从各自的模块导入（`noeta.core.*`、`noeta.toolkit.*`、
`noeta.plugins.*`、`noeta.backends.*`、`noeta.host.*`、`noeta.agents`）；`noeta.cli` 是 `noeta`
命令本身，不属于 SDK。完整示例见[在 Python 里嵌入](../start/embedding.md)；写插件那一侧见
[Extension 接口](./extension.md)。

## 哪些接口是稳定的

noeta 遵循[语义化版本](https://semver.org/lang/zh-CN/)。下面这些是公开接口，次版本和补丁版本都不会破坏它们：

- 能从 `noeta` 直接导入的所有名字（`noeta.__all__`，也就是本页列的）；
- [`noeta` 之外常用的模块](#noeta-之外常用的模块)里列出的模块和名字；
- [Extension 接口](./extension.md)里的插件接口，以及[事件](./events.md)里的事件类型；
- 这几个 entry point 组：`noeta.plugins`、`noeta.providers`、`noeta.execenvs`、`noeta.stores`、
  `noeta.channels`、`noeta.frontends`。

`noeta` 下的其余部分（`noeta.core` 的其他模块、`noeta.toolkit`、`noeta.plugins`、`noeta.host`、
`noeta.agents`、`noeta.backends`，以及 `noeta.cli`）都是内部实现：能导入，但任何版本都可能改；
以 `_` 开头的名字也一样。后端要实现的协议（`Store`、`Provider`、`ExecEnv`）还没定死：次版本可能给它们加必须实现的成员，
changelog 里会写明。

要去掉的公开名字先标为弃用：照常能用，但会发出 `DeprecationWarning`，并在 changelog 的
*Deprecated* 一节里至少挂一个次版本；只有主版本才会真正删掉它。

## 构建各个部分

### `noeta.store(url) -> Store`

按地址的 scheme 通过 `noeta.stores` entry point 找到存储：`sqlite:///path`（会展开 `~`，自动建
上级目录）、`postgres://…` 或 `postgresql://…`（需要 `postgres` 组件）。不认识的 scheme 抛
`ValueError`。

### `noeta.provider(family, **kw) -> Provider`

按类型找 provider 适配器（`noeta.providers` entry point：`anthropic`、`openai_compat`、
`openai_responses`，或第三方的）。关键字参数交给它的工厂函数；内置的接受 `api_key=`、`base_url=`
和 `extra_headers=`。`openai_compat` 必须给 `base_url`（接口根地址，后面会拼上 `/chat/completions`，比如 `…/v1`）；`openai_responses` 必须
同时给 `base_url`（完整的 `…/v1/responses` 地址）和 `api_key`。不给 `api_key` 时，`anthropic` 读
`ANTHROPIC_API_KEY`，`openai_compat` 读 `OPENAI_API_KEY`；`anthropic` 还接受
`prompt_cache_ttl="1h"`。

### `noeta.default_agents(model, *, plugins=None, add=None, extensions=(), extra_files=(), made=None, **main_kw) -> list[Agent]`

`main` 和子代理 `Explore`、`Plan`、`general-purpose`，由 `noeta/agents/files/` 里的代理文件构建，
再加上 `extra_files` 里的代理（和内置代理同名的会替换掉内置的）。

| 参数 | 含义 |
|---|---|
| `model` | 所有代理用的模型，除非代理文件另有指定；写了 `model: inherit`（或没写 `model`）的代理用它，作为子代理时用调用方的模型 |
| `plugins` | 按插件名：它的参数（映射；`enabled: False` 表示关掉）、一个现成的 `Extension`，或 `False`（从所有代理里去掉）。`memory`、`mcp`、`repetition_guard` 要在这里写上设置才开 |
| `add` | 按代理名（`"*"` 表示全部）：在文件自带的插件之后再加，写成 `(名字, 参数)` 对 |
| `extensions` | 加给每个代理的 `Extension` 实例 |
| `extra_files` | 更多 `AgentFile`（来自 `read_agent_dir` / `parse_agent_file`） |
| `made` | 一个字典，记住创建过的插件，第二次构建时复用没变的 |
| `**main_kw` | 要替换的 `main` 字段，如 `one_shot=True`、`system_prompt=…`、`name=…` |

### `noeta.parse_agent_file(text, *, source="", default_name="") -> AgentFile`

读一个代理文件：YAML frontmatter 用 Claude Code 的字段（外加 `plugins`、`one_shot` 和 `identity`），正文是系统
提示词。文件格式不对时抛 `AgentFileError`。

### `noeta.read_agent_dir(path) -> list[AgentFile]`

`path` 下的每个 `*.md` 代理文件（不递归）。读不了的文件记日志后跳过。

### `noeta.Agent`

一个 dataclass，是 `Runtime` 运行的对象。

| 字段 | 默认 | 含义 |
|---|---|---|
| `name` | — | 代理名，任务里记的就是它 |
| `model` | — | 模型 id |
| `system_prompt` | `""` | 系统提示词（提示词名 `system/<agent>`） |
| `identity` | `""` | 一行身份说明，作为单独的缓存块放在系统提示词前面 |
| `description` | `""` | 它在别的代理的子代理清单里那一行 |
| `extensions` | `()` | 它的插件 |
| `tools` | `()` | 插件之外它自己的工具 |
| `effort` | `None` | `low` … `max` |
| `max_tokens` | `None` | 输出上限；`None` 取模型目录里的 |
| `one_shot` | `False` | 第一轮结束就结束任务，而不是等下一条消息 |
| `output_schema` | `None` | 任务没设时，回答要遵循的 JSON Schema |
| `step_budget` | `1000` | 一轮里允许调用模型的次数；用完后的下一次要求关掉工具给出最终回答 |
| `extra` | `{}` | 代理文件的其他字段，原样保留 |
| `allowed_tools` | `None` | 只提供这些工具名（`None` 表示全部） |
| `denied_tools` | `()` | 永远不提供的工具名 |

`all_extensions()` 返回任务实际运行的插件列表：`extensions`，前面再加上把 `tools` 包成的
`ToolSet`；有两个插件同名或有插件没名字时抛 `ValueError`。

## 运行任务

### `noeta.Runtime`

即 worker：从存储里认领任务并运行，可以同时跑很多个。一个 dataclass：

| 字段 | 默认 | 含义 |
|---|---|---|
| `store` | — | 存储 |
| `agents` | — | `dict[名字, Agent]` |
| `provider_for` | — | `模型 id -> Provider` |
| `env_for` | 本机、任务的 `cwd` | `TaskState -> ExecEnv`：任务的工具在哪个执行环境里跑 |
| `task_provider` | `None` | `(state, model) -> Provider`，用来遵循任务配置里记下的路由 |
| `prepare_child` | `None` | 子任务的 `TaskCreated` 写入之前由宿主补全 |
| `worker` | `主机:pid:随机串` | 这个 worker 在认领记录里的名字 |
| `queues` | `("default",)` | 从哪些队列认领 |
| `lease_seconds` | `30.0` | 一次认领不续约能维持多久 |
| `poll_interval` | `0.5` | 没有变化通知时，多久看一次有没有可认领的任务（秒） |
| `concurrency` | `8` | 同时跑几个任务 |
| `spill_root` | `None` | 太长、没法直接返回的工具输出存在哪；`None` 表示截断 |
| `prompt_dirs` | `()` | 宿主的提示词覆盖文件目录 |
| `workspace_trusted` | `None` | `cwd -> bool`：任务的 `.noeta/prompts` 能不能覆盖提示词（`None` 表示都能） |
| `on_delta` | `None` | 实时输出回调 `(task_id, Delta) -> None` |
| `output_ceiling` | 模型目录给的 | 代理没设时 `模型 -> max_tokens` |
| `snapshot_every` | `100` | 每隔多少个事件写一次重放快照；`0` 表示不写 |
| `restart_limit` | `3` | 上一个认领没交还就重启、期间一步没做完，最多连续这么多次；再崩一次任务以 `failed` 结束；`0` 表示不限 |
| `agent_aliases` | `{}` | 代理的旧名字 -> 现在的名字，给用旧名记录的任务用 |
| `roots` | `None` | 只服务这些任务树（一个集合，前端往里加）；`None` 表示服务所有任务 |
| `sleep` | `asyncio.sleep` | 模型调用重试之间怎么等（测试里换成假的） |

方法：

| 方法 | 作用 |
|---|---|
| `await run_until_idle()` | 不断认领并运行任务，直到没有可认领的 |
| `await run_once() -> bool` | 认领一个任务，跑到它开始等待或结束；没有可认领的返回 `False` |
| `await serve(stop: asyncio.Event)` | 有任务可认领就跑，直到 `stop` 被设置；退出时正在跑的任务会交还 |
| `await let_go()` | 放掉在等待期间保留着的认领（任务持有后台作业时）；任务仍处于等待状态 |
| `await preview(task_id) -> ModelRequest \| None` | 任务下一次调用模型时的请求起点，不记录任何东西 |
| `serve_root(root_id)` | 把一棵任务树加进 `roots`，并立刻找活干 |
| `agent(name) -> Agent \| None` | 按名字或旧名字找代理 |
| `provider_of(state, model) -> Provider` | 任务这次调用发给哪个 provider：设了 `task_provider` 就用它，否则用 `provider_for` |
| `await run_claim(claim)` | 运行调用方已经拿到的一次认领，直到任务开始等待或结束 |

### `noeta.LocalApp(store, agents, provider, *, own_tasks_only=False, task_config=None, channels=(), **runtime_kw)`

存储、在后台跑的 `Runtime` 和客户端，全在一个进程里：终端界面、`noeta acp`、`noeta -p`、`noeta worker` 用的就是它
（`noeta/host/local.py`）。

| 参数 | 含义 |
|---|---|
| `agents` | `Agent` 列表 |
| `provider` | 一个 `Provider`，或 `模型 -> Provider` 函数 |
| `own_tasks_only` | 只服务自己创建或接手的任务树；新任务建在 `front-end` 队列上 |
| `task_config` | 垫在每个新任务配置下面（`prompts` 按键合并） |
| `channels` | 启动后一起运行的渠道（`Channel`） |
| `**runtime_kw` | 传给 `Runtime`（`spill_root` 默认是系统临时目录下的 `noeta-<uid>/tool-results`；`poll_interval` 默认 0.2，客户端也按这个间隔轮询；调用方自己传的值优先） |

| 成员 | 说明 |
|---|---|
| `client` | 一个 `LocalClient`（`Client` 的子类），它创建的新任务由这个应用的 worker 执行；它把 `task_config` 垫在每个新任务下面，并把代理文件的 `initialPrompt` 放在新对话第一条消息前面 |
| `runtime`、`store` | 各个部分 |
| `await start(worker=True)` / `await stop()`，或 `async with` | 在后台运行 worker、渠道，以及每天一次的旧工具输出清理；停止时也会关闭存储 |
| `await run_until(stop)` | 先 `start()`，一直跑到 `stop`（一个 `asyncio.Event`）被设上，或者 worker 自己出错（错误照常抛出），然后 `stop()`：只跑任务的进程用它，`noeta worker` 就是这样 |
| `deltas(sink) -> 取消订阅函数` | 实时输出：每个片段调用一次 `sink(task_id, Delta)`，用 Postgres 时也包括其他机器的 |
| `await adopt(task_id)` | 接手一个已有任务的任务树（续聊时用）；这个应用没有那个代理时抛 `UnknownAgent` |
| `replace_agents(agents)` | 从下一次接手任务起改用这些代理 |
| `await agent_names()` | 所有代理的名字，排好序 |
| `await commands(agent, ctx)`、`await run_command(agent, ctx, text)` | 某个代理的斜杠命令 |
| `await context(task_id)` | 任务下一次请求里装了什么，按类别统计 |
| `await summaries(limit=50, archived=None)`、`await archive(task_id, archived=True)` | 给续聊列表用的根任务 |
| `await rewind(task_id, point, *, conversation=True, files=True) -> Rewound` | 回到某条用户提问之前（`point` 是 `noeta.host.rewind.rewind_points` 给出的 `RewindPoint`）：对话、文件，或者两样都回退。`RemoteApp` 也有这个方法，交给服务端去做（`POST /tasks/{id}/rewind`），所以 TUI 和 ACP 适配器连 `--server` 时回退走的是同一条路 |
| `notices`、`main_agent` | 前端该展示给人的提示；默认用的代理（`"main"`） |
| `channels` | 它运行的渠道 |
| `reload_plugins`、`list_models`、`prepare_model` | 由宿主设置的回调（`noeta` 命令会设）：重新查找插件、列出各 provider 的模型、切换模型前先把它定下来；没设时为 `None` |

### `noeta.Client(store, *, poll_interval=0.5)`

本进程里、建在存储上的客户端接口（`noeta/core/client.py`）。它只写存储，活由某处的 `Runtime` 干。

| 方法 | 作用 |
|---|---|
| `await create(agent, input=None, *, task_id=None, config=None, queue="default", dedupe_key=None, history_from=None, history_through=None) -> str` | 创建任务，可以顺带第一条消息；返回任务 id。重复的 `dedupe_key` 或已存在的 `task_id` 返回已有的任务 |
| `await send(task_id, input, *, when="next_turn", dedupe_key=None) -> int` | 发消息（`"now"`：在正在进行的这一轮的下一步送到）；返回它在收件箱里的序号 |
| `await answer(task_id, request_id, decision=None, *, always=False, data=None) -> int` | 回答审批（`"allow"` / `"deny"`）或提问（`data`）；同一个请求 id 幂等 |
| `await signal(task_id, name, data=None, *, dedupe_key=None, wake=True) -> int` | 发一个带名字的信号给插件读（`permission_mode`、`config` 等） |
| `await cancel(task_id, reason="", *, turn_only=False)` | 结束任务，或只停下当前这一轮 |
| `await get(task_id) -> TaskRow` | 任务行；不存在时抛 `UnknownTask` |
| `await tasks(*, parent_id=None, status=None, limit=100, roots_only=False) -> list[TaskRow]` | 任务表里的行，最近更新的在前（`roots_only`：只列没有父任务的任务） |
| `await events(task_id, after=0) -> list[Recorded]` | 序号 `after` 之后的日志 |
| `watch(task_id, after=0) -> AsyncIterator[Recorded]` | 从 `after` 开始实时吐出日志，任务结束为止 |
| `await wait(task_id, *, timeout=None) -> TaskRow` | 等到任务开始等待或已结束 |

`input` 是字符串，或一个 `noeta.core.types.Message`（由 `Text` 和 `Image` 块组成的用户发言）；
图片在存下之前会缩到合适的大小。`history_from` 指向的任务不存在时抛 `UnknownTask`。

### `noeta.RemoteClient(url, *, token=None, transport=None)`

方法和 `Client` 相同，走 `noeta serve` 服务端的 HTTP 接口（`noeta/host/remote.py`）；`watch` 会
自动重连。它还有 `await context(task_id)`（任务下一次请求里装了什么）、`await rewind(task_id, point, *, conversation=True, files=True)`（调服务端的 `POST /tasks/{id}/rewind`，新任务只带请求允许设置的配置）和 `await close()`（关闭它的
HTTP 客户端）。`transport` 是 `httpx` 的 transport（测试用）。

### `noeta.connect(url, *, token=None, **kw) -> Client | RemoteClient`

`sqlite:///…` 或 `postgres://…` 给出这个存储上的 `Client`；`http://…` / `https://…` 给出连那台
服务端的 `RemoteClient`（带 `token`）。多余的关键字参数交给客户端的构造函数。

## 记录和状态

| 名字 | 说明 |
|---|---|
| `noeta.Recorded` | 存下来的一个事件：`seq`、`at`（Unix 时间）、`event`（`noeta.core.events` 里十种事件之一；见[事件](./events.md)） |
| `noeta.TaskState` | 重放日志得到的任务状态：配置、消息、调用、等待、子任务、定时器、用量，以及每个插件自己的状态（在 `ext` 里，插件只读） |
| `noeta.ToolUse` | 模型发起的一次工具调用：`id`、`name`、`input` |

## 写插件和工具

下面是写插件要用的名字；每个的完整说明见 [Extension 接口](./extension.md)和[写一个插件](../guides/plugins.md)。

| 名字 | 签名 | 说明 |
|---|---|---|
| `noeta.Extension` | 继承它，设置 `name` | 唯一的插件接口：工具、上下文、提醒、命令、工具调用前的把关、从日志重放出的状态；每个方法都可选 |
| `noeta.TaskView` | 传给各个钩子 | 插件眼中的任务：重放出的状态、记录条目的方法、执行环境 |
| `noeta.ToolChanges` | `(add=(), withhold={}, entries=())` | `Extension.tool_changes` 对任务工具列表做的改动 |
| `noeta.ContextPart` | `(text, order=100, before="")` | 插件往系统提示词里加的文字 |
| `noeta.Reminder` | `(text, kind="reminder", data=None, origin="system", role="user", order=100)` | 给模型看、但不是用户说的话 |
| `noeta.Command`、`noeta.CommandContext` | `(name, description, template="", argument_hint="", local=False)`；`(cwd, task_id=None)` | 前端提供的斜杠命令，以及它在哪里列出、执行 |
| `noeta.Allow`、`noeta.Ask`、`noeta.Deny`、`noeta.Rewrite` | `Allow(approve=False, outright=False)`、`Ask(prompt="", detail={}, firm=False)`、`Deny(reason="")`、`Rewrite(input, name=None)` | `before_tool` 的返回值：拒绝优先于改写、询问和放行 |
| `noeta.Tool` | 一个协议：`name`、`description`、`input_schema`、`read_only`、`clearable`、`async run(input, ctx)` | 模型能调用的工具；可选 `flags`、`resume`、`recover`、`rule_inputs` |
| `noeta.ToolContext` | 传给 `run` | 这次调用的任务、执行环境、各插件状态，以及 `record`、`spawn`、`post`、`keep`、`progress` 等 |
| `noeta.ToolOutput` | `(content=[], is_error=False, meta={}, display=None, park=None, end_turn=None, block_list=False)` | 工具的结果 |
| `noeta.text_output` | `(text, *, is_error=False, **meta) -> ToolOutput` | 纯文字结果 |
| `noeta.function_tool` | `@function_tool` 或 `@function_tool(name=None, description=None, read_only=False, clearable=False, deferred=False)` | 把带类型标注的函数变成工具：名字、docstring、由标注生成的参数格式 |
| `noeta.ask` | `ask(*questions) -> Park` | 让工具调用挂起，向人提问（`{"question", "header"?, "options"?, "multiSelect"?}`） |
| `noeta.read_answer` | `read_answer(outcome, asked) -> dict \| None` | 挂起的提问拿到的回答（已校验），撤回时为 `None` |

## `noeta` 之外常用的模块

| 模块 | 用途 |
|---|---|
| `noeta.core.events` | 事件类型（`TaskCreated`、`ModelReplied`、`ToolStarted`、`Waiting`、`TaskEnded` 等）和收件箱条目 |
| `noeta.core.types` | `Message`、`Text`、`Image`、`ToolUse`、`Delta`、`Usage` |
| `noeta.core.models` | 模型目录：`find_spec`、`register_models`、`ModelSpec`、`ALIASES` |
| `noeta.host.pending` | `pending_tree`、`choices`、`answer_choice`：一棵任务树在等人处理什么 |
| `noeta.host.questions` | `answer_question`：校验后回答提问 |
| `noeta.host.http` | `create_app`：把 HTTP 接口作为 Starlette 应用嵌进你自己的服务 |
| `noeta.testing` | `ScriptedProvider`、`reply`：测试用的按脚本回复的模型；`noeta.testing.store_contract`：每种存储都要通过的那套测试 |
| `noeta.core.registry` | `load_plugin_files`、`load`、`available` 和各个组名（`PLUGINS` 等）：entry point 和插件文件 |
| `noeta.backends.stores.sqlite`、`noeta.backends.execenvs.local`、`noeta.backends.execenvs.container` | `SqliteStore`、`LocalExecEnv`、`ContainerExecEnv` 和 `start_container`：按类名直接用自带的存储和执行环境 |
