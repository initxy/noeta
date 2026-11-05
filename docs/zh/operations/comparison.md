# 和其他 agent 对比

这一页把 noeta 的插件能力和另外三个 agent 逐项对照：Claude Code（noeta 照着它的行为做）、pi 和 DeepSeek Harness（dsh）。移植插件时可以拿来查，也可以看 noeta 故意没做哪些、为什么。noeta 怎么检查"模型看到的内容和 Claude Code 发的一致"，是另一页的事：[如何对齐 Claude Code](../how-it-works/parity.md)。

## 一句话版本

| | Claude Code | pi | DeepSeek Harness（dsh） | noeta |
|---|---|---|---|---|
| 运行方式 | 每个会话一个本地进程 | 单进程 | 单进程 | 一个存储上挂任意多个工人，也可以单进程 |
| 任务状态在哪 | 本地会话里 | 进程里，加上会话条目 | 进程里，加上会话事件 | 存储里任务的日志；一切状态都从日志算出来 |
| 另一台机器能否接着跑 | 不能 | 不能 | 不能 | 能，按日志接着跑 |
| 插件用什么写 | markdown、JSON、shell / HTTP hook、MCP | TypeScript | TypeScript（Cordis） | Python，外加 Claude Code 的 markdown、JSON、hook、MCP 格式 |
| 任务用哪些插件 | 加载了什么就是什么 | 加载了什么就是什么 | 加载了什么就是什么 | 任务开始时记下来；缺插件的工人绝不跑它 |
| 默认往外发数据 | 开了分析时有遥测 | 安装遥测、provider 归属头 | 会话日志、插件清单、匿名 id（走官方 API 时） | 不发 |

下面是细节。只有 noeta 这一列的说法在多台机器上依然成立：状态从任务日志算出来，插件集按任务固定，任何工人都能接着跑任何任务；pi 和 dsh 只在一个进程里跑，Claude Code 只在一个本地会话里跑。

## 写一个插件的步骤，和 pi 并排看

从零开始，写一个工具加一个事件处理：

| 步骤 | noeta | pi |
|---|---|---|
| 1. 安装 | `uv tool install --with 'noeta-tui @ git+https://github.com/initxy/noeta#subdirectory=tui' 'noeta @ git+https://github.com/initxy/noeta'` | `npm install -g @earendil-works/pi-coding-agent` |
| 2. 模型密钥 | `NOETA_API_KEY` / `ANTHROPIC_API_KEY`，或 `config.toml` 里的 `api_key` | `/login`，或 provider 的密钥环境变量 |
| 3. 写一个文件 | `~/.noeta/plugins/x.py`：一个带 `add_to = ("main",)` 的 `Extension`，一行 `from noeta import …` | `~/.pi/agent/extensions/x.ts` |
| 4. 运行 | `noeta`，已开着的输入 `/reload-plugins` | `pi`，或 `/reload` |
| 别处的文件，只试一次 | `noeta --plugin-dir ./x.py` | `pi -e ./x.ts` |

两边都是 4 步，就地试一个文件都只要 1 条命令。在 noeta 里加一个技能只要 1 步（在 `~/.noeta/skills` 里放一个 `SKILL.md` 目录）。noeta 这边的完整过程见[写一个插件](../guides/plugins.md)。

## 逐项对比

对照的版本：pi-mono e7a0df2（coding-agent 1.4.0）、DeepSeek Harness f3803c96（0.2.2-alpha.2）、Claude Code 2.1.312–2.1.314。结论分三种：**已有**（noeta 覆盖了，形式可能不同）、**新增**（为这次对照专门做的）、**不做**（故意不做）。noeta 一列写的是对应代码，每个方法见 [Extension 接口](../reference/extension.md)。

| 扩展点 | pi | dsh | Claude Code | noeta | 结论 | 原因 |
|---|---|---|---|---|---|---|
| **生命周期** | | | | | | |
| 任务开始 | `session_start` | `session/created`、`agent/created` | SessionStart | `on_start`；hook SessionStart | 已有 | 这时抓到的内容会记下来，任务恢复后照样发。 |
| 任务结束 | `session_shutdown` | `session/disposed` | SessionEnd | `on_event`（`TaskEnded`）；hook SessionEnd | 已有 | 结束就是日志里的一条事件。 |
| 每追加一条事件 | — | `session/event` | — | `on_event` | 已有 | 指标、审计、桥接都可以边长边读日志。 |
| 一轮、一次模型调用、空闲 / 忙碌 | `turn_*`、`agent_*`、`agent_settled` | `agent/status` | — | `on_event`：`InputTaken`、`ModelCalled`、`ModelReplied`、`Waiting` | 已有 | 每个都是日志里的事件，不需要第二条消息总线。 |
| 一轮快结束时让它继续 | `agent_before_settle`、`turn_end` | `agent/turn-stopping` | Stop、SubagentStop | 在 `on_event` 里看到 `ModelReplied` 就投一条 `Say(when="now")`；hook Stop | 已有 | "继续"是收件箱里的一条消息，所以崩溃后也还在。 |
| 改写或拦下用户输入 | `input` | `agent/pre-step` | UserPromptSubmit | `reminders` + `view.erase_messages` + 抛 `TurnFailed`；hook UserPromptSubmit | 已有 | 被拦下的输入由一条内核记录抹掉（ADR-026）。 |
| 压缩前 / 后，自己写摘要 | `session_before_compact`、`session_compact` | 一个 `CompactionEngine` | PreCompact、PostCompact | `before_compact`（返回文字或 `Deny`）；`on_event`（`Compacted`）；替换压缩的插件记 `view.compacted` | 已有 | 压缩本身是个插件；其他插件通过 `before_compact` 和 `resident` 参与。 |
| 上下文超长 | — | `agent/request-error` | 内置 | `on_context_overflow(view, request)` | 新增 | hook 直接拿到超长的那个请求，插件不必在内存里按任务各存一份。 |
| 加载了说明文件 | — | — | InstructionsLoaded | hook InstructionsLoaded | 新增 | Claude Code 有：每显示一个说明文件触发一次。 |
| 切换了模型或思考力度 | `model_select`、`thinking_level_select` | `agent-preset/selected` | PostModelSwitch | `on_event`（`InputTaken`）；hook PostModelSwitch | 已有 | 切换是日志里的一条输入。 |
| 否决分叉、切换或树上移动 | `session_before_fork`、`_switch`、`_tree` | `sessions.fork` | `/rewind`，无 hook | 分叉就是新建一个任务（`history_from`）；`continue_state` 决定带走什么 | 不做 | 分叉从不改动原任务，没有需要否决的东西。 |
| 插件授予项目信任 | `project_trust` | — | 信任对话框 | `noeta trust` | 不做 | 信不信任由人决定；能授予信任的代码等于在放大代码自己的权限。 |
| Claude Code 的其他 hook 事件 | — | — | PreModelSwitch、Setup、TeammateIdle、Elicitation、ConfigChange、FileChanged、Worktree… | 警告并跳过（`noeta/plugins/hooks/config.py`） | 不做 | 每个都属于 noeta 没有的某个 Claude Code 功能。 |
| 加载器、热更新、插件市场、web 服务的事件 | — | Cordis `loader/*`、`hmr/*`、`plugin-market/*`、`webserver/*` | — | entry points、HTTP 接口 | 不做 | 那些是 dsh 自己的加载器和 web 服务，不是 agent 的行为。 |
| **模型请求** | | | | | | |
| 修改单个请求（消息、系统提示） | `context`、`context_with_system` | `agent/pre-step`、`system-prompt/assemble` | — | `before_model` 改 `ModelRequest`；`TurnDone`、`RequestStale` | 已有 | 请求始终只由日志和这些 hook 决定。 |
| 请求头 | `before_provider_headers` | `deepseekLlmApiExtensions` | 自定义请求头的环境变量 | `before_model` 里设 `ModelRequest.headers`；settings 的 `extra_headers` | 已有 | 每个请求单独设，来自 hook。 |
| 改写发给 provider 的原始报文 | `before_provider_request` | `llm/stream` | — | — | 不做 | 请求必须能从日志重现；线上格式的改动放进 provider。 |
| 原始响应、流式增量 | `after_provider_response`、`provider_stream_event` | `agent/assistant-stream` | — | 通过 `on_event` 拿 `ModelReplied`；增量只给前端 | 不做 | 增量从不落盘：插件据此行动，就是在依据日志里没有的东西行动。 |
| 替换最终回复 | `message_end` | — | — | — | 不做 | 记下来的回复就是事实；要改就改下一个请求。 |
| 缓存预热 | `cache_warming_decision` | — | — | — | 不做 | 每个空闲任务都调一次，就打破了"等待不花钱"；缓存断点已经能起作用。 |
| 用 hook 定重试策略 | — | `agent/request-error` | 内置 | 记进日志的内核重试（`noeta/core/retry.py`） | 不做 | 重试在每个工人上都一样，并且在日志里可见。 |
| **工具** | | | | | | |
| 注册工具、运行中改变工具集 | `registerTool` | `tools.register` | MCP 服务器、SDK `tool()` | 每一步调 `tools(view)`；`function_tool` | 已有 | 工具集可以一步一变。 |
| 覆盖或包装内置工具 | 同名者胜；`create*Tool` | `tools.setExecutor` | PreToolUse `updatedInput` | `Rewrite`、`after_tool`、`view.find_tool`；关掉内置工具所在的插件 | 已有 | 先出现的名字胜出，所以替换必须明说，不会悄悄顶掉。 |
| 拦截一次调用 | `tool_call` | `tools/pre-execute` | PreToolUse、规则 | `before_tool` → `Allow` / `Deny` / `Rewrite` / `Ask` | 已有 | Deny > Rewrite > Ask > Allow；改写后的调用会再过一遍。 |
| 用代码回答询问 | — | `approval/request` | PermissionRequest、`canUseTool` | `resolve_ask` | 已有 | 在调用停下来等之前就回答掉。 |
| 修改结果 | `tool_result` | `tools/post-execute` | PostToolUse | `after_tool` | 已有 | 在结果记进日志之前改。 |
| 包住整个调用过程 | — | `tools/execute` | — | `before_tool` + `after_tool` | 不做 | 重试属于工具自己（`recover`、`retry_safe`），计时看 `ToolStarted` / `ToolFinished`。 |
| 延迟加载的工具 | `exposure` | `when` | `defer_loading` + ToolSearch | `ToolFlags.deferred`；`tool_changes` | 已有 | 等 `ToolSearch` 加载后才发给模型。 |
| 并行或放最后 | `executionMode` | `isConcurrencySafe` | 只读工具一起跑 | `Tool.read_only`、`ToolFlags.run_last` | 已有 | 同一条回复里的只读调用一起跑。 |
| 工具运行中的进度 | `onUpdate` | — | Bash 实时输出、`tool_progress` | `ToolContext.progress` | 新增 | Claude Code 有；它和实时输出一起推送，从不记日志。 |
| 工具里调模型 | `modelRegistry.complete` | `ctx.llm` | WebFetch 的小模型 | `ToolContext.complete` | 新增 | `WebFetch` 的摘要走它，调用换到别的工人上照样能用任务的模型。 |
| 工具里调另一个工具 | `ctx.executeTool` | — | — | `view.find_tool(name).run(…)` | 已有 | 不再过一遍拦截（`by_model` 为 false）。 |
| 等人或等子任务 | 阻塞的 `ui.confirm` | 审批、提问 | AskUserQuestion | `ask()`、`ToolOutput.park` | 已有 | 等待期间任务不占工人。 |
| 单次调用的崩溃恢复 | — | — | — | `Tool.recover`、`ToolFlags.retry_safe`、`ctx.idempotency_key` | 已有 | 只有 noeta 有：一次调用绝不会在不知情的情况下跑两遍。 |
| 后台工作 | — | `jobs.start` | 后台 Bash | `ctx.keep()` | 已有 | 任务一直留在后台工作所在的那个工人上。 |
| 给模型的结构化输出 | `outputSchema` | `output.schema` | MCP `structuredContent` | `ToolOutput.meta`，给前端的 `display` | 不做 | 模型读的是内容；结构随结果一起给前端。 |
| 调用和结果的渲染代码 | `renderCall`、`renderResult` | UI 插槽 | — | `ToolOutput.display`（数据） | 不做 | 前端不导入任何插件，可能在远程，也可能是别的语言写的。 |
| shell 环境变量 | `spawnHook` | `shellEnv.register` | settings `env` | settings `env`；按调用 `Rewrite` | 已有 | 用 Claude Code 的设置键。 |
| **命令与参数** | | | | | | |
| 斜杠命令 | `registerCommand` | `commands.register` | `commands/*.md`、技能 | `commands` / `run_command`；`commands/*.md` | 已有 | 前端列出并展开它们（`noeta/core/commands.py`）。 |
| 参数补全 | `getArgumentCompletions` | — | `argument-hint` | `Command.argument_hint` | 已有 | 提示是数据；回调函数到不了远程前端。 |
| 用命令驱动整个会话 | 命令上下文 | `ctx.sessions` | 仅内置命令 | 客户端接口 | 不做 | 新建、分叉、重新加载属于前端（通过客户端接口），不属于任务里的代码。 |
| 快捷键 | `registerShortcut` | `shortcuts.register` | 用户自己的 `keybindings.json` | — | 不做 | 插件从不伸手进前端；Claude Code 也是让人、而不是插件来绑键。 |
| 插件自己的命令行参数 | `registerFlag` | `cmdlineArgs` | — | `[plugins.<name>]`、代理文件参数 | 不做 | 命令行参数只活在一个进程里；插件参数按任务固定，每个工人上都一样。 |
| 只为这一次运行加载插件 | `pi -e` | `--patch`（可以加插件的配置叠加层） | `--plugin-dir` | `--plugin-dir` | 新增 | Claude Code 有。 |
| **上下文** | | | | | | |
| 系统提示词文字 | `systemPromptOptions` | `systemPrompt.section` | 输出风格、`--append-system-prompt` | `ContextPart` | 已有 | 放在那一整块缓存的系统提示里。 |
| 替换系统提示词 | `SYSTEM.md` | `system-prompt/assemble` | `--system-prompt` | 提示词覆盖 `system/<agent>`；代理文件正文 | 已有 | 覆盖在任务开始时记下来，恢复后发的字节不变。 |
| 每轮的附加说明 | `sendMessage(nextTurn)` | `agent.inject` | `<system-reminder>` | `reminders` | 已有 | 记进日志，任务恢复后照样发。 |
| 一轮中途插话 | `sendUserMessage` | `agent.steer` | 流式输入 | `view.post`、`Say(when="now")` | 已有 | 是收件箱里的一条消息，下一步时取走。 |
| 技能、提示词模板、说明文件 | `SKILL.md`、`prompts/`、`AGENTS.md` | `skills.register`、代理说明 | 技能、命令、`CLAUDE.md` | 技能、`markdown_commands`、说明文件（`NOETA.md`、`AGENTS.md`） | 已有 | 用 Claude Code 的格式，放在 noeta 自己的位置（ADR-004）。 |
| 代码插件附带技能、命令、代理 | `resources_discover` | `skills.registerProvider` | 插件的各个目录 | Python 插件目录里的 `skills/`、`commands/`、`agents/` | 新增 | Python 插件目录可以把它们和代码放在一起。 |
| 压缩后保留的文字 | — | — | 内置 | `resident`、`lead_resident` | 已有 | 做成接口，任何插件的文字都能保留。 |
| **界面** | | | | | | |
| 对话框 | `ui.select`、`confirm`、`input` | 审批、提问 | AskUserQuestion、权限提示 | `ask()`、`Ask` | 已有 | 每个前端都能显示，不需要插件写任何前端代码。 |
| 通知 | `ui.notify` | toast 插槽 | Notification hook | `view.notify(text, kind, data)` | 已有 | 不会以用户的身份进到模型那里。 |
| 面板、小部件、编辑器、主题 | `ui.*` | 106 个 UI 插槽 | mods、主题 | — | 不做 | 前端不导入插件；插件发布数据，由前端渲染。 |
| 状态栏 | `setStatus` | — | `statusLine` 设置 | — | 不做 | 留给插件：记录和 `notify` 带着前端可以展示的内容。 |
| 插件自己的 HTTP 路由 | — | `webServer.register` | — | — | 不做 | 只有一套 API；外部流量通过渠道 `Channel` 进来。 |
| **状态** | | | | | | |
| 插件条目，可从历史重建 | `appendEntry` | 会话事件、投影 | — | `view.record` + `reduce`、快照 | 已有 | 插件状态都是从日志算出来的。 |
| 给条目打标签 | `setLabel` | — | — | — | 不做 | Claude Code 没有对应的东西；插件可以自己记。 |
| 定时器 | — | `ctx.schedule` | 定时唤醒 | `view.set_timer` | 已有 | 定时器是一条记录，由存储在任意工人上触发。 |
| 读别的任务 | `SessionManager.list` | `sessionQuery` | — | `read_log`、`child_tasks`、`ctx.tasks` | 已有 | 只读。 |
| 存储后端 | — | `SessionPersistence` | — | `noeta.stores`、`store_contract` | 已有 | 用共用的契约测试检查。 |
| **模型** | | | | | | |
| provider（线上协议） | `registerProvider` | `llm.registerAdapter` | — | 一个 `noeta.providers` 包 | 已有 | 和内置的一样，按名字通过 entry point 找到。 |
| 多个 provider，模型在它们之间切换 | `models.json` 里的 providers；`/model` 跨 provider 切换 | 未核对 | 一次一个 provider；`/model` 在它的模型间切 | `[providers.<name>]` 表；`/model`、stream-json 的 `set_model` 和 ACP 都能跨 provider 切；选中的 provider 和模型上限记在任务配置里 | 新增 | owner 要求任何模型都能用、任务中途能切；把路由记下来，任务恢复后在每个工人上都走同一个 provider。 |
| 插件文件里带 provider | 支持 | 支持 | — | — | 不做 | 插件固定机制不覆盖 provider；只存在于某个工人上的 provider 会破坏"任何工人都能接着跑任何任务"。 |
| 模型和它走哪种接口 | `models.json` | `registerModelDiscovery` | — | `register_models`、模型文件；接口种类不限 | 新增 | 第三方 provider 自己声明接口种类（`family`）；只有 anthropic 和 openai 带约定。 |
| 登录型 provider | `oauth` | DeepSeek 账号 | `/login` | 密钥、`{env:}` / `{keyring:}` | 不做 | 服务端用的是运维方的密钥；订阅登录是桌面端的事。 |
| 按请求路由 | `registerVirtualModel` | `llm/stream` | — | `provider_for`；`before_model` 设 `model` | 已有 | 由 SDK 宿主程序做。 |
| **子代理** | | | | | | |
| 子代理定义 | 一个示例 | `subagents.registerProvider` | `agents/*.md` | 代理文件、`subtasks` | 已有 | 用 Claude Code 的代理文件格式。 |
| 每个代理自己的插件、传给子代理的配置、队列 | — | 代理预设 | 每个代理自己的工具、MCP、hook | 代理的 `plugins`、`inherit`、`spawn(queue=)` | 已有 | 子代理可以跑在另一台机器的队列上。 |
| 把别的 agent 当子代理 | — | codex、claude-code、ACP | — | — | 不做 | 子代理是这个存储里的一个任务；别的 agent 可以做成工具或 MCP 服务器。 |
| **打包** | | | | | | |
| 放一个文件就能被找到 | `~/.pi/agent/extensions` | — | 插件目录 | `~/.noeta/plugins`、已信任的 `.noeta/plugins` | 已有 | 项目里的代码只有在信任之后才加载。 |
| 包与安装 | npm、git、`pi install` | `dsh plugin add` | `/plugin install` | entry points；`noeta plugin add <git>@<commit>` | 已有 | 安装时插件里的东西一行都不跑。 |
| 托管的插件市场、自动更新 | 插件市场、`pi update` | 插件市场 | marketplaces、自动更新 | 读 Claude Code 的 `marketplace.json` | 不做 | 那是一个需要运营的产品，也是供应链风险；改为固定版本安装。 |
| 热更新 | `/reload` | HMR | `/reload-plugins` | 两轮之间 `/reload-plugins`；滚动重启 | 已有 | 绝不在一轮中间：请求必须始终由日志决定。 |
| 按任务固定插件集 | — | — | — | `records.Plugins` | 已有 | 只有 noeta 有：缺插件的工人绝不会缺着插件跑这个任务。 |
| 插件之间的总线或服务 | `pi.events` | Cordis 服务 | — | 只通过内核 | 不做 | 状态留在日志里，插件可以一个个单独替换。 |
| agent 加载自己写的插件 | — | 动态 Cordis | — | 写 `.noeta/` 要询问 | 不做 | 否则模型的输出就能把代码装进运行它的程序里。 |
| 进程内插件代码的沙箱 | — | — | 通过 API 隔离的 mods | 执行环境、MCP 服务器、命令型 hook | 不做 | CPython 里做不到；在真能隔离的地方提供隔离。 |
| 插件导入的名字 | `ExtensionAPI` | 带类型的 `ctx` | — | `from noeta import Extension, Tool, Allow, Deny, Ask, Rewrite, ToolUse, Recorded, ToolChanges, TaskState, …` | 新增 | 插件文件只需一行导入。 |
| **配置** | | | | | | |
| 插件配置、密钥 | 设置键、`$ENV` | 配置 schema | `userConfig`、钥匙串 | `[plugins.<name>]`；`{env:}`、`{keyring:}` | 已有 | 密钥只在构造插件时解析。 |
| Claude Code 插件的 `userConfig`、`settings.json` | — | — | 启用时询问；`agent` | `noeta plugin add` 时询问；`agent` | 新增 | Claude Code 有。 |
| 读别人的设置、实时配置 | `getSettings` | `ctx.settings`、`Volatile` | — | 只读插件自己的参数 | 不做 | 一个插件不应依赖另一个插件的设置；改设置就是重新加载，并按任务固定。 |
| 监视器（monitors）、`bin/` | — | — | 插件组成部分 | — | 不做 | 每个会话一个常驻进程，和"等待时不占工人"的任务模型对不上（改用渠道或定时器）；`bin/` 会悄悄放大 Bash 能跑的东西。 |
| LSP 服务器 | — | — | 插件组成部分 | — | 不做 | 语言服务器在一台机器上持有一个检出目录的状态；把检查器做成在执行环境里跑的工具（`examples/run-tests`），或放在 MCP 服务器后面。 |
| 输出风格 | — | — | 插件组成部分 | 任务开始时记下的提示词文字（`examples/house-style`） | 不做 | noeta 没有风格开关；风格就是插件加的一段提示词。 |
| 主题 | — | — | 插件组成部分 | — | 不做 | 颜色归各个前端管。 |
| **默认往外发的数据** | 安装遥测和 provider 归属头（`PI_TELEMETRY`） | 走官方 API 时：会话日志（`dsh_session_log`）、插件清单（`dsh_plugin_packages`）和一个匿名用户 id | 开了分析时有遥测 | 不发 | 不做 | 永远不做。运维方想要的话，可以自己装一个基于 `on_event` 的导出插件。 |

## "不做"的那些，背后是同几条规则

noeta 故意不做的东西，大多落在下面四条规则里，每一条都源于"任务是一条任何工人都能接着跑的日志"：

- **请求只由日志决定。** 发给模型的字节，只要日志在别的工人上重现不出来，就不允许改：不改写原始报文，不依据流式增量行动，不替换已记录的回复，不在一轮中间热更新。
- **插件不伸手进前端。** 前端可能在远程、用别的语言写、同时开好几个，所以插件只发布数据（记录、`notify`、`ToolOutput.display`），怎么显示由各个前端决定：没有面板、快捷键、主题或渲染回调。
- **除非用认领把任务留住，否则不让任何东西只活在一个进程里。** 每个会话一个的监视器、LSP 服务器、只在插件里的 provider，都会把任务绑死在一台机器上；改用渠道、定时器、执行环境或 `ctx.keep()`。
- **凡是放大代码权限的事，都由人来决定。** 插件不能授予信任；模型写 `.noeta/` 都要询问（所以它没法自己装上自己写的插件）；安装固定版本，并且不执行插件里的任何东西。
