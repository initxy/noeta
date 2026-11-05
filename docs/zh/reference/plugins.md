# 内置插件

noeta 自带的每个插件：做什么、哪些代理在用、加了哪些工具和命令、实现了 `Extension` 的哪些方法、有哪些配置项。内置插件和第三方插件一样，在 `pyproject.toml` 的 `[project.entry-points."noeta.plugins"]` 下登记。想自己写一个，见[写一个插件](../guides/plugins.md)。

插件的配置项就是它构造函数的关键字参数。写在 `~/.noeta/config.toml` 的 `[plugins.<name>]` 下，或写在代理文件的 `plugins` 列表里（`- <name>: {key: value}`）。每个插件都接受 `enabled = false`，对所有代理关掉它。标了 **SDK** 的配置项要传 Python 对象（函数、transport、客户端），只能在宿主代码里给（`noeta.default_agents(plugins=…)`、`build_agents`）。

## 哪些代理用哪些插件

内置代理就是 `noeta/agents/files/` 下的几个文件。插件文件的 `add_to`，或 `[agents.<name>] add_plugins`，可以往里再加。

| 插件 | `main` | `general-purpose` | `Explore`、`Plan` | 说明 |
|---|---|---|---|---|
| `ask_user` | ✓ | | | 只在问题有人能回答时提供 |
| `budget` | ✓ | ✓ | ✓ | 只计设了上限的那几项 |
| `checkpoints` | ✓ | ✓ | | |
| `compaction` | ✓ | ✓ | ✓ | 总是排在列表最后 |
| `empty_reply` | ✓ | ✓ | ✓ | |
| `environment` | ✓ | ✓ | ✓（`git_status: false`） | |
| `findings` | ✓ | | | |
| `fs` | ✓ | ✓ | ✓（`tools: [Read]`） | |
| `hooks` | ✓ | ✓ | ✓ | |
| `instructions` | ✓ | ✓ | ✓ | |
| `markdown_commands` | ✓ | ✓ | ✓ | 由 `noeta` 命令加给所有代理 |
| `mcp` | ✓ | ✓ | ✓ | 不设置就不开；`noeta` 命令会设置 |
| `memory` | ✓ | | | 不设置就不开；`noeta` 命令会设置 |
| `permissions` | ✓ | ✓ | ✓ | |
| `plan_mode` | ✓ | ✓ | ✓ | |
| `quiet_nudge` | ✓ | | | 只有部分 Claude 模型会收到提醒 |
| `repetition_guard` | ✓ | ✓ | ✓ | 设了 `[plugins.repetition_guard]` 才开 |
| `schedule` | ✓ | | | |
| `shell` | ✓ | ✓ | ✓ | |
| `skills` | ✓ | ✓ | ✓ | |
| `structured_output` | ✓ | ✓ | ✓ | 所有由代理文件构建的代理都有；任务没有 schema 时什么也不做 |
| `subtasks` | ✓（`agents: ["*"]`） | ✓（`agents: ["*"]`） | ✓（没有 `Agent` 工具） | |
| `title` | ✓ | | | |
| `todos` | ✓ | | | 默认只有较老的 Claude 模型有这些工具 |
| `tool_search` | ✓ | ✓ | ✓ | 所有由代理文件构建的代理都有 |
| `web` | ✓ | ✓ | ✓ | 有搜索密钥才有 `WebSearch` |
| `workflow` | ✓ | | | |

"不设置就不开"（`noeta/agents/__init__.py` 的 `OFF_UNLESS_SET`）：`memory` 要往某个目录里写，`mcp` 要启动给它的服务器，重复调用检查又不是 Claude Code 有的东西，所以 `default_agents` 在宿主没给设置之前一律关着。`noeta` 命令会给 `memory` 和 `mcp` 设置（`noeta/cli/config.py`），所以终端界面、`-p`、`noeta worker`、`noeta serve` 里这两个都是开的；`repetition_guard` 仍然关着。没写 `plugins` 的代理文件（普通的 Claude Code 代理文件）用 `general-purpose` 那一套（`DEFAULT_PLUGINS`）。`tool_search` 和 `structured_output` 会自动加到每个由代理文件构建、又没写它们的代理上（`ALWAYS_PLUGINS`）。

`noeta` 命令还会从 noeta 的目录和 settings 文件里填一些配置项：`memory.home`、`hooks.settings_files`、`mcp.settings_files` 和 `mcp.project`、`skills` 的各个目录和清单预算、`instructions.user_dir` 和 `managed_dir`、`permissions` 的规则和模式、`plan_mode` 的配置、`fs.write_roots`（额外目录）、`environment.language` 和署名行。你在 `config.toml` 里写的表按键覆盖在这些之上。

## 工具和命令一览

| 插件 | 模型看到的工具 | 斜杠命令 |
|---|---|---|
| `ask_user` | `AskUserQuestion` | |
| `compaction` | | `/compact` |
| `findings` | `ReportFindings` | |
| `fs` | `Read`、`Write`、`Edit`、`NotebookEdit`（延迟加载） | |
| `hooks` | | `/hooks` |
| `markdown_commands` | | `/init`、`/review`，以及每个 `commands/*.md` |
| `mcp` | `mcp__<server>__<tool>`（延迟加载）、`ListMcpResourcesTool`、`ReadMcpResourceTool`、`ReadMcpResourceDirTool`（延迟加载，只给顶层任务）；需要登录的服务器提供 `mcp__<server>__authenticate` / `complete_authentication` | `/mcp`，以及每个服务器提示词 |
| `plan_mode` | `EnterPlanMode`、`ExitPlanMode`（都延迟加载） | `/plan` |
| `schedule` | `ScheduleWakeup`；`CronCreate`、`CronList`、`CronDelete`（延迟加载） | `/loop` |
| `shell` | `Bash`；`Monitor`、`TaskStop`、`EnterWorktree`、`ExitWorktree`（延迟加载） | |
| `skills` | `Skill`；`scripts = true` 时还有 `run_skill_script` | 每个技能一个 `/<skill>` |
| `structured_output` | `StructuredOutput`（仅当任务有输出 schema） | |
| `subtasks` | `Agent`、`ListAgents`（只给顶层任务）、`SendMessage`（延迟加载）；由 `Agent` 启动的任务里还有 `SubagentHandback` | |
| `todos` | `TaskCreate`、`TaskGet`、`TaskUpdate`、`TaskList`（延迟加载） | |
| `tool_search` | `ToolSearch`（只要有延迟加载的工具就提供） | |
| `web` | `WebFetch`（延迟加载）；`WebSearch`（延迟加载，需要密钥） | |
| `workflow` | `Workflow` | |

延迟加载的工具只把名字告诉模型，等 `ToolSearch` 加载后才把完整定义发过去（每个工具的参数见[内置工具](./tools.md)）。其余插件不加工具，它们通过提示词、提醒、关卡和记录起作用。

## 逐个说明

### `ask_user`

`noeta/plugins/ask_user`：在问题有人能回答的地方提供 `AskUserQuestion`，也就是有人在实时跟进的任务（`interactive` 不是 `false`），或客户端会代答提示的任务（`permission_prompt_tool`）。已经问出去的问题，即便条件变了也还能回答。子任务继承父任务"能不能问"的设定（`inherit`）。没有配置项。

实现的方法：`tools`、`inherit`。

### `budget`

`noeta/plugins/budget.py`：给任务设终身上限：模型调用次数、工具调用次数、花费、启动的子代理数、子代理嵌套深度（Claude Code 的 SDK 有 `maxTurns` 和 `maxBudgetUsd`）。超出上限的调用会被拒绝。计数从任务日志算出来，所以每个工人执行的上限都一样。

实现的方法：`before_model`、`before_tool`、`after_tool`、`on_event`、`reduce`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `max_iterations` | 无 | 模型调用次数；`max_turns` 是它的别名 |
| `max_turns` | 无 | 同 `max_iterations` |
| `max_tool_calls` | 无 | 工具调用次数 |
| `max_cost_usd` | 无 | 花费，按模型目录里的价格算 |
| `max_spawned_subtasks` | 无 | 启动的子代理数，按 `spawn_tools` 的调用计 |
| `max_subtask_depth` | `3` | 子代理最多嵌套几层 |
| `spawn_tools` | `["Agent", "Task"]` | 哪些工具算启动子代理 |

### `checkpoints`

`noeta/plugins/checkpoints.py`：给回退用的文件存档。`Edit`、`Write`、`NotebookEdit` 运行前，先把文件内容记下来，每个文件每次输入只记一次，按内容哈希存（同样的内容一个任务只存一份）。前端读 `snapshot` 记录（`noeta/host/rewind.py`），发 `restore_files` 信号，通过任务的执行环境把文件写回去，不管任务跑在哪。只能恢复这个任务的日志（以及它接续的对话、它们的子代理）存过档的内容。和 Claude Code 一样，通过 `Bash` 做的改动不记录。没有配置项。

实现的方法：`on_event`、`reduce`。

### `compaction`

`noeta/plugins/compaction`：让长任务一直装得进模型的上下文窗口。接近阈值、输入 `/compact`、或 provider 报告超长时，把对话压成摘要，再把最近读过的文件放回来。任务恢复时必须给同样的配置，因为它们决定请求的字节。原理见[上下文、缓存与压缩](../how-it-works/context.md)。

实现的方法：`commands`、`run_command`、`reminders`、`on_start`、`before_model`、`on_event`、`reduce`、`continue_state`、`on_context_overflow`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `auto_compact_window` | `$NOETA_AUTO_COMPACT_WINDOW`，否则模型的窗口 | 计算阈值所用窗口的上限（token） |
| `auto_compact_percent` | `$NOETA_AUTOCOMPACT_PCT_OVERRIDE` | 当它比默认阈值低时，在有效窗口的这个百分比处触发 |
| `auto` | `true` | 自动压缩（`/compact` 总是可用） |
| `compaction_model` | 任务的模型 | 只用于生成摘要的另一个模型 |
| `restore_files` | `true` | 摘要之后重新读一遍最近用过的文件 |
| `read_tool` | 任务的 `Read` | **SDK**：用哪个工具重新读 |
| `transcripts` | `true` | 每次压缩时把整段对话写到 `<home>/projects/<workspace>/<task id>.jsonl`（只限本地工作区），摘要里指向它 |
| `home` | noeta 的主目录 | 这些记录写到哪 |
| `auto_compact_precompute` | `true` | 从有效窗口约 80% 起就在后台写摘要，到阈值时直接换上 |
| `auto_compact_clear_results` | `false` | 距上次模型调用超过 `auto_compact_clear_idle_seconds`（缓存已经凉了）时，清掉可清理工具较早的结果 |
| `auto_compact_clear_idle_seconds` | `3900` | 上一项的空闲时长 |
| `auto_compact_idle` | `false` | 用一小时提示缓存时，在缓存过期前压缩空闲的大任务 |
| `name` | | **SDK**：插件名 |
| `clock` | | **SDK**：测试用 |

### `empty_reply`

`noeta/plugins/empty_reply.py`：Claude Code 的空回复提醒。既没文字也没工具调用的回复（只有推理也算空）不保留，并让模型继续，每步一次；已经提醒过、此后又没有新内容，这一轮就不带回答地结束。没有这个插件时，第一个空回复就结束这一轮。没有配置项。

实现的方法：`on_empty_reply`。

### `environment`

`noeta/plugins/environment`：Claude Code 告诉模型"你在哪里运行"的那些内容：第一条用户消息里的 git 快照和提交署名说明，紧随其后作为系统消息的环境信息（模型、延迟加载的工具、token 预算、今天的日期），以及每次输入和工具结果后的 `<total_tokens>` 预算提示。全部通过任务的执行环境采集，任务第一次开始时记下来，压缩时再采一次，中间不变，不破坏提示缓存。只有 Claude Code 认得的 Claude 模型才收到预算提示。

实现的方法：`reminders`、`on_start`、`before_model`、`on_event`、`reduce`、`inherit`、`resident`、`lead_resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `model` | | 代理的模型，任务配置没写模型时用它 |
| `model_name` | `true` | 带上模型那一行 |
| `git_status` | `true` | 在第一条用户消息里放 git 快照 |
| `git_refresh` | `true` | 分支或 HEAD 变了，下一轮开始时再给一次快照 |
| `attribution` | `true` | 放提交 / PR 署名说明 |
| `attribution_commit`、`attribution_pr` | | settings 的 `attribution.commit` / `attribution.pr`（`""`：不署名） |
| `language` | | settings 的 `language`：让模型用这种语言回答 |
| `token_budget` | `15000000` | 每轮开始的预算；`0`：不发预算提示 |
| `date_change` | `true` | 日期变了就告诉模型 |
| `today` | 任务 `timezone` 下的日期，否则 UTC | **SDK**：返回 `YYYY-MM-DD` 的函数 |
| `critical_reminder` | `""` | 每次调用模型都带上的文字（代理文件的 `criticalSystemReminder_EXPERIMENTAL`） |

### `findings`

`noeta/plugins/findings.py`：`ReportFindings`，把代码审查的发现以结构化数据交给前端。它们作为 `core` / `notice` 下 `findings` 类型的记录到达用户那里；模型只被告知记了几条。没有配置项。

实现的方法：`tools`。

### `fs`

`noeta/plugins/fs`：文件工具 `Read`、`Write`、`Edit`、`NotebookEdit`，都通过任务的执行环境运行；用户消息里的 `@路径` 会把那些文件附上。写到哪里能不能写，和 Claude Code 一样由权限那一层决定。

实现的方法：`tools`、`reminders`、`before_model`、`before_tool`、`after_tool`、`on_event`、`reduce`、`continue_state`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `tools` | 全部四个 | 提供哪些文件工具，按名字写（只读代理写 `[Read]`） |
| `write_roots` | `[]` | 工作目录之外还允许写入的目录（Claude Code 的 `additionalDirectories`） |
| `block_writes_outside` | `false` | 不管什么模式、在问任何人之前，直接拒绝写到工作目录和 `write_roots` 之外（`Bash` 不受这道围栏管，那是沙箱的事） |
| `mentions` | `true` | 附上用户用 `@路径` 点名的文件 |
| `home` | 不设 | 用户的主目录，`@~/…` 指向它；用户机器上的前端会设，服务端不设 |

### `hooks`

`noeta/plugins/hooks`：Claude Code 的 hook：`command`、`http`、`prompt`、`agent`、`mcp_tool` 和客户端回调，在任务的固定节点上运行。noeta 触发的事件：`PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`PostToolBatch`、`UserPromptSubmit`、`Stop`、`StopFailure`、`SubagentStart`、`SubagentStop`、`SessionStart`、`SessionEnd`、`PreCompact`、`PostCompact`、`PostModelSwitch`、`PermissionRequest`、`PermissionDenied`、`Notification`、`CwdChanged`、`InstructionsLoaded`。Claude Code 的其他事件配了也会被跳过并给出警告（`noeta/plugins/hooks/config.py`）。怎么写 hook 见[权限与 hooks](../guides/permissions.md)。

实现的方法：`commands`、`run_command`、`reminders`、`on_start`、`before_tool`、`resolve_ask`、`after_tool`、`on_event`、`reduce`、`continue_state`、`inherit`、`before_compact`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `hooks` | | Claude Code 的 `hooks` 对象 |
| `settings_files` | `[]` | 把这些 settings 文件里的 `hooks` 也加进来（`noeta` 命令会传入它读的 settings 文件） |
| `permission_prompt_tool` | | 一个 MCP 工具（`mcp__<server>__<tool>`），代替用户回答所有权限请求，同 `--permission-prompt-tool` |
| `callbacks` | | **SDK**：已连接的客户端注册并回答的 hook |
| `runner`、`http_transport` | | **SDK**：测试用 |

### `instructions`

`noeta/plugins/instructions`：说明文件，按 Claude Code 的层次、放在 noeta 自己的位置：`NOETA.md`（托管、用户、项目、本地四层），支持 `@path` 引入和 `.noeta/rules`；跨工具通用的 `AGENTS.md`；自动记忆的索引。见[项目说明与记忆](../guides/memory-and-instructions.md)。

实现的方法：`on_start`、`on_event`、`reduce`、`inherit`、`lead_resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `root` | `true` | 对话开始时记下这些文件 |
| `file` | | 只读这个路径，不按目录逐级查找 |
| `discovery` | `true` | 工具碰到某个文件时，显示对它适用的说明文件 |
| `filenames` | `["AGENTS.md"]` | 目录里没有 `NOETA.md` / `.noeta/NOETA.md` 时读这些（第一个非空的） |
| `user_dir` | 无 | 用户目录（`~/.noeta`）：它的 `NOETA.md` 和 `rules/` |
| `managed_dir` | 无 | 机器的托管目录 |
| `local` | `true` | 读 `NOETA.local.md` |
| `walk` | `true` | 工作目录的上级目录也读 |
| `rehang` | `true` | 压缩后把这些文件放回摘要前面 |
| `origin` | `"system"` | 记下来的消息的来源标记 |
| `managed_only` | `false` | 只读托管目录的文件（代理文件的 `omitClaudeMd`） |

### `markdown_commands`

`noeta/plugins/markdown_commands.py`：用 markdown 写的斜杠命令（Claude Code 的 `commands/*.md`）。输入 `/<name> <参数>` 会加上填好参数的正文，正文里的 `` !`命令` `` 行要过权限检查，`allowed-tools` 列出的工具被预先放行。自带 `/init`（写 `NOETA.md`）和 `/review`（审查改动）。

实现的方法：`commands`、`before_tool`、`on_event`、`reduce`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `commands` | `[]` | 每个命令一张表：`name`、`body`，可选 `description`、`argument_hint`、`allowed_tools` |
| `builtin` | `true` | 也提供 `/init` 和 `/review` |

### `mcp`

`noeta/plugins/mcp`：来自 MCP 服务器的工具、资源和提示词，传输方式有 stdio、Streamable HTTP、SSE、WebSocket。每轮开始时拉一次工具列表、记下来，这一轮内不变；任务恢复后工具不变；凭据永远不进日志。用法见 [MCP 服务器](../guides/mcp.md)。

实现的方法：`tools`、`commands`、`run_command`、`reminders`、`on_start`、`on_event`、`reduce`、`resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `servers` | `[]` | Claude Code `mcpServers` 格式的服务器；宿主代码里也可以是一个按任务求值的函数 |
| `settings_files` | `[]` | 把这些 settings 文件里的 `mcpServers` 也加进来 |
| `project` | `false` | 同时加载任务 `<cwd>/.noeta/mcp.json` 中、它自己的 settings 同意了的服务器；宿主代码里可以是按目录判断的函数 |
| `agent_servers` | | 代理文件自己的 `mcpServers`，只给这个代理的任务用 |
| `scope` | 一个共享分区 | **SDK**（或一个字符串）：给共享连接分区，有状态的服务器不会把一个用户的状态带进另一个用户的任务 |
| `pool` | 每个工人进程一个 | **SDK**：连接池（空闲 30 分钟的连接会关掉） |

### `memory`

`noeta/plugins/memory`：Claude Code 的自动记忆：一个 markdown 文件目录，模型用 `Write` / `Edit` 写，`MEMORY.md` 是索引，位置在 `<home>/projects/<workspace>/memory/`。任务开始时读 `MEMORY.md`（前 200 行、25,000 字符以内），压缩后再读一次。见[项目说明与记忆](../guides/memory-and-instructions.md)。

实现的方法：`context`、`reminders`、`on_start`、`after_tool`、`reduce`、`continue_state`、`inherit`、`lead_resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `home` | `$NOETA_HOME` 或 `~/.noeta` | noeta 的主目录 |
| `directory` | | 改用这个目录 |
| `recall_model` | 关 | 每条用户消息都问这个模型该附上哪些记忆文件（最多五个） |
| `rehang` | `true` | 压缩后再读一次 `MEMORY.md` |
| `agent_scope`、`agent_name` | | 代理文件的 `memory` 字段：用这个代理自己的目录（`user`、`project` 或 `local`） |

### `permissions`

`noeta/plugins/permissions`：Claude Code 的权限系统，管每一次工具调用：各种模式、`allow` / `deny` / `ask` 规则、工作目录和受保护路径、自动模式的分类器，外加声明式的 PreToolUse 规则和 `can_use_tool`。deny 规则压过一切，其次是 ask 规则，再次是 allow 规则。用法见[权限与 hooks](../guides/permissions.md)。

实现的方法：`reminders`、`on_start`、`before_model`、`before_tool`、`resolve_ask`、`on_event`、`reduce`、`continue_state`、`inherit`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `mode` | `"auto"` | 起始权限模式（`defaultMode`） |
| `allow`、`deny`、`ask` | `[]` | 规则字符串：`Bash(git push *)`、`Read(./.env)`、`WebFetch(domain:example.com)`、`mcp__server` |
| `additional_directories` | `[]` | 文件工具可以像在工作目录里一样读写的其他目录 |
| `auto_mode` | | 给分类器用的 Claude Code `autoMode` 对象（`allow`、`soft_deny`、`hard_deny`、`environment`） |
| `disable_bypass_mode` | `false` | 拒绝 `bypassPermissions` |
| `classifier_model` | Claude 任务用 `claude-sonnet-5`，否则任务的模型 | 自动模式分类器用的模型 |
| `plans_dir` | `<home>/plans` | 计划文件目录，读写都不询问 |
| `trusted_workspace` | `false` | "总是允许"的回答也写进项目的 `.noeta/settings.local.json`；说明文件对分类器算作用户意图（宿主代码里可以是函数） |
| `remember_to_project` | `true` | 已信任时是否做上面这种写入 |
| `shell_allowlist` | `[]` | `{rule = "git push *"}` 条目：`Bash(...)` allow 规则的简写 |
| `webfetch_allowed_hosts` | `[]` | `example.com` / `*.example.com`：`WebFetch(domain:...)` allow 规则的简写 |
| `risk`、`default_risk`、`max_risk` | `{}`、`"high"`、无 | 哪些工具需要权限（`low` 不需要），以及一个上限，高于它的工具直接拒绝 |
| `allowed_tools`、`disallowed_tools` | | 按名字筛选提供的工具（从请求里拿掉，被调用也拒绝）；禁掉 `Bash` 也会拿掉 `Monitor` 和 `TaskStop` |
| `pre_tool_use` | `[]` | 声明式 PreToolUse 规则（`noeta/plugins/permissions/guards.py`） |
| `can_use_tool` | | **SDK**：`(tool, input) -> bool`，代替人回答询问 |

### `plan_mode`

`noeta/plugins/plan_mode`：Claude Code 的计划模式：`/plan`、`EnterPlanMode`、`ExitPlanMode`（批准计划的那个问题）、计划文件和计划模式下的提示。模式本身记在权限插件的 `mode` 记录里；没有权限插件时，这个插件自己保证计划阶段只读。

实现的方法：`tools`、`commands`、`run_command`、`reminders`、`on_start`、`before_tool`、`on_event`、`reduce`、`inherit`、`resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `plans_dir` | `<home>/plans` | 计划文件放哪 |
| `auto_during_plan` | `true` | 在自动模式下开始的计划，用自动模式的分类器来判断（`useAutoModeDuringPlan`） |

### `quiet_nudge`

`noeta/plugins/quiet_nudge.py`：Claude Code 的"太久没说话"提醒：连续好几步只调工具、没给用户任何文字时，提醒模型有进展就告诉用户一声。和 Claude Code 一样，只有 `claude-opus-5-5`、`claude-sonnet-5-5`、`claude-fable-5-1`、`claude-mythos-5-1` 会收到。

实现的方法：`reminders`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `steps` | `5` | 连续多少步只调工具才提醒 |
| `most` | `3` | 一段沉默最多提醒几次 |

### `repetition_guard`

`noeta/plugins/repetition_guard.py`：某次调用和紧挨着的前几次一模一样（同一工具、同样参数），连同自己算达到 `threshold` 次，就询问或拒绝。它读的是对话本身，所以每个工人看到的连续调用都一样。Claude Code 没有这种检查，所以不设 `[plugins.repetition_guard]` 就不开。

实现的方法：`before_tool`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `threshold` | `3` | 连续相同调用的次数；`<= 0` 表示关 |
| `action` | `"require_approval"` | `require_approval` 问用户，`deny` 直接拒绝 |
| `window` | `8` | 看最近多少次调用 |

### `schedule`

`noeta/plugins/schedule`：和 Claude Code 一样的定时提示：`ScheduleWakeup` 控制自定节奏的 `/loop`；`CronCreate` / `CronList` / `CronDelete` 按 cron 计划或只跑一次地发送提示；`/loop` 两种都能设。每个唤醒和定时任务都是任务日志里的一个内核定时器，所以重启后还在，由持有这个任务的工人触发。见[子代理与后台任务](../guides/subagents.md)。没有配置项（`clock` 是测试用的）。

实现的方法：`tools`、`commands`、`on_start`、`on_event`、`reduce`、`continue_state`。

### `shell`

`noeta/plugins/shell`：`Bash`（工作目录在多条命令之间保持），以及后台任务：`run_in_background`、`Monitor`、`TaskStop`；`EnterWorktree` / `ExitWorktree` 把 shell 切进 `.noeta/worktrees/` 下的 git worktree 再切回来。后台任务属于工人进程：它在跑的时候，任务一直留在那个工人上。见[子代理与后台任务](../guides/subagents.md)和[命令在哪里执行](../guides/sandbox.md)。

实现的方法：`tools`、`reminders`、`on_start`、`on_event`、`reduce`、`continue_state`、`resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `background` | `true` | 提供后台任务（`run_in_background`、`Monitor`）；关掉后只有前台 `Bash`（`TaskStop` 仍在，用来停后台代理和 Workflow） |
| `background_ends_with_turn` | `false` | 一轮结束就结束这个任务的后台任务（一次性工人用） |
| `bash` | `{}` | 给 `Bash` 的参数：`timeout_s`、`output_cap`、`shell_env`、`hardening_rules` |
| `max_jobs` | 每个任务 8 个 | 和 `log_dir` 一起用，改用一张私有的后台任务表，而不是进程共用的那张 |
| `log_dir` | | 那张表把后台任务日志放哪 |
| `registry` | 进程共用的表 | **SDK**：后台任务表 |

### `skills`

`noeta/plugins/skills`：来自 `SKILL.md` 目录的技能：模型看到的技能清单、`Skill` 工具、每个技能一个 `/<name>`、`run_skill_script`（开了 `scripts` 时）以及 `allowed-tools` 的执行。用法见[技能、代理与命令](../guides/skills-and-agents.md)。

实现的方法：`tools`、`commands`、`reminders`、`on_start`、`before_model`、`before_tool`、`after_tool`、`on_event`、`reduce`、`continue_state`、`resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `dirs` | `[]` | 最低一层：自带的和借用的技能目录 |
| `plugin_dirs` | | 目录 → 插件名：Claude Code 插件的技能，名字是 `<插件>:<技能>` |
| `global_agents_dir`、`global_dir` | 无 | 用户的 `~/.agents/skills` / `~/.noeta/skills` |
| `global_commands_dir` | 无 | 用户的 `~/.noeta/commands`：每个 `*.md` 是一个技能 |
| `managed_dir` | 无 | 机器的托管目录，它的技能排最前 |
| `workspace` | `true` | 工作目录（及其上级目录，直到仓库根）的 `.noeta/skills`、`.agents/skills`、`.noeta/commands` |
| `additional_dirs` | `[]` | 任务的其他目录，排在工作区之后 |
| `skills_dir` | | 用这一个目录代替工作区那几层，不受信任检查 |
| `plugin_vars`、`plugin_commands` | | Claude Code 插件的变量和命令 |
| `trusted` | 不限制 | **SDK**：`trusted(path) -> bool`，判断仓库里那几层能不能加载 |
| `budget_chars` | | 清单预算（字符数；`noeta` 命令里是 `NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET`） |
| `budget_fraction` | `0.01` | 不设上一项时，按模型窗口的这个比例（`skillListingBudgetFraction`） |
| `max_desc_chars` | `1536` | 每条说明的长度上限（`skillListingMaxDescChars`） |
| `rank`、`usage_from` | | **SDK**：给清单排序；`noeta` 命令按它的存储里各任务的使用情况排 |
| `preload` | `[]` | 任务一开始就启用的技能 |
| `scripts` | `false` | 提供 `run_skill_script` |
| `tool_enforcement` | `"off"` | 已启用技能的 `allowed-tools`：`off`、`approval`（询问）或 `deny` |
| `rehang` | `true` | 压缩后把清单和已启用的技能放在摘要后面 |
| `origin` | `"system"` | 记下来的清单和技能正文的来源标记 |
| `shell_commands` | `true` | 技能加载时运行正文里的 `` !`命令` `` 行 |

### `structured_output`

`noeta/plugins/structured_output.py`：任务配置里设了 `output_schema`（`noeta -p --json-schema`、HTTP 请求的配置、workflow 的 `agent({schema})`）时，提供一个输入结构就是这个 schema 的 `StructuredOutput` 工具。调用符合 schema，就以这个值作为回答结束这一轮；不符合，返回一个写明哪里不对的错误。没有 schema 的任务里它什么也不做。

实现的方法：`tools`、`on_start`、`before_model`、`on_event`、`reduce`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `output_schema` | | 任务配置和代理都没设时用的 schema |
| `max_retries` | `5` | 一轮里被拒多少次调用后这一轮失败 |

### `subtasks`

`noeta/plugins/subtasks`：子代理。`Agent` 启动一个子任务（后台或前台），`SendMessage` 给正在跑的子代理发消息，`ListAgents` 列出它们，由 `Agent` 启动的子任务里有 `SubagentHandback`。见[子代理与后台任务](../guides/subagents.md)。

实现的方法：`tools`、`reminders`、`on_start`、`before_model`、`before_tool`、`on_event`、`reduce`、`continue_state`、`resident`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `agents` | `[]` | 模型能启动哪些代理类型；`"*"` 表示除自己之外所有带描述的代理；一个都没有就不提供 `Agent` 工具 |
| `roster` | `[]` | **SDK**：以 `SubagentSpec` 给出的代理类型 |
| `max_depth` | `3` | 到这个深度的任务不能再派活（根任务是 0） |
| `max_background` | `20` | 一个任务同时最多跑几个子代理 |
| `exclusive_with` | `[]` | 不能和 `Agent` 调用出现在同一条回复里的工具名 |
| `inherit_config` | `["cwd", "model", "effort"]`（`main` 还加上 `permission_mode`） | 子任务从父任务配置里复制哪些键；`model` 和 `effort` 只在调用和代理定义都没设时才复制 |
| `queue` | 调用方的队列 | 子任务建在哪个队列上 |
| `progress_labels` | `true` | 运行中的子代理每 30 秒让模型给一个 3–5 个词的标签，说明它在干什么，只给前端看 |

### `title`

`noeta/plugins/title.py`：像 Claude Code 给会话起名那样，给任务起一次名：取第一条至少 10 个字符的输入，单独发一个小请求。没人实时跟进的任务（`interactive: false`）不起名。前端读 `title` 记录；用户用 `rename` 信号改名。

实现的方法：`on_event`、`reduce`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `model` | 任务的模型 | 起名这个小请求用的模型 |

### `todos`

`noeta/plugins/todos`：Claude Code 的任务清单：`TaskCreate`、`TaskGet`、`TaskUpdate`、`TaskList`，以及连续十轮没用它们时的提醒。和 Claude Code 2.1.312 一样，默认只有比它精简版提示词更老的 Claude 模型（以及 Bedrock 的应用推理配置 application inference profile）才有这些工具；Claude 5 系列和其他所有模型都没有。只有没有父任务的任务才有。

实现的方法：`tools`、`context`、`reminders`、`reduce`、`continue_state`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `all_models` | `false` | 给所有模型都提供（Claude Code 的 `CLAUDE_CODE_ENABLE_TODO_TOOLS`） |

### `tool_search`

`noeta/plugins/tool_search`：延迟加载工具。只要任务提供了延迟加载的工具，它就加上 `ToolSearch`，并把还没加载的那些从请求里拿掉；某个工具一旦在工具结果里被点名就算加载了，这件事会记下来，压缩后也不会丢。没有配置项。

实现的方法：`tool_changes`、`after_tool`、`reduce`。

### `web`

`noeta/plugins/web`：`WebFetch`（取网页，用任务的模型或一个更便宜的模型，按调用里的 `prompt` 给出摘要）和 `WebSearch`（走 Tavily 搜索 API，有密钥才提供）。

实现的方法：`tools`、`on_event`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `webfetch_model` | 任务的模型 | 给网页做摘要的模型 |
| `search_api_key` | `$NOETA_WEB_SEARCH_API_KEY` | Tavily 密钥；两者都没有就不提供 `WebSearch` |
| `digest_timeout` | `60` | 摘要超过这么多秒就放弃（返回原始网页） |
| `digest`、`fetch_transport`、`search_transport` | | **SDK**：替换摘要调用或 HTTP transport（测试、代理、沙箱出口） |

### `workflow`

`noeta/plugins/workflow`：`Workflow`。模型按 workflow API（`agent()`、`parallel()`、`pipeline()`、`phase()`、`log()`、`args`）写一段 JavaScript 脚本；每个 `agent()` 是一个子任务，整个运行在后台进行，日志里只存结果，所以重启后还在，也能换工人接着跑。Claude Code 的 `ultracode` 开关也在这里。见[子代理与后台任务](../guides/subagents.md)。

实现的方法：`tools`、`reminders`、`on_start`、`on_event`、`reduce`、`continue_state`。

| 配置项 | 默认 | 含义 |
|---|---|---|
| `default_agent` | `"general-purpose"` | `agent()` 跑的代理类型，没有就用任务自己的 |
| `max_running` | CPU 核数减 2，限制在 2 到 16 之间 | 每次运行同时跑几个代理 |
| `home` | `~/.noeta` | noeta 的目录，存着用户保存的 workflow |
| `engine` | | **SDK**：测试用 |

## 其他 entry point 组

插件只是 noeta 按名字查找的六组之一（`noeta/core/registry.py`）。其他几组里 noeta 自带的：

| 组 | 名字 | 是什么 |
|---|---|---|
| `noeta.providers` | `anthropic`、`openai_compat`、`openai_responses` | 模型 API（[模型与 provider](../guides/models.md)） |
| `noeta.execenvs` | `local`、`container` | 命令在哪里跑（[命令在哪里执行](../guides/sandbox.md)） |
| `noeta.stores` | `sqlite`、`postgres`（也认 `postgresql`） | 任务存在哪（[作为服务端运行](../guides/server.md)） |
| `noeta.frontends` | `tui`（来自 `noeta-tui` 包） | 终端界面 |
| `noeta.channels` | 无内置 | 外部对话；`examples/chat-bridge` 是一个例子 |
