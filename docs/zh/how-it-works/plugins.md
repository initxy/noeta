# 插件体系

除了"把任务日志跑起来"这件事，noeta 做的其余一切都是插件：文件工具、shell、权限、hooks、技能、子代理、MCP、记忆、压缩、环境信息和项目说明、延迟加载工具。这一页讲它们共同实现的那一个接口、每个钩子在什么时候被调用、插件怎么保存能扛住崩溃又能换机器继续用的状态、插件怎么被找到和配置，以及任务怎么记下自己用了哪些插件。想直接动手看[写一个插件](../guides/plugins.md)；每个方法的签名见 [Extension 接口参考](../reference/extension.md)。

## 只有一个接口

一个能力插件就是 `Extension`（`noeta/core/extension.py`）的一个子类。所有方法都是可选的：只加一个工具的插件只实现 `tools`，只管放行与拦截的插件只实现 `before_tool`。provider、执行环境、存储和渠道（channel）属于后端，各有自己的小接口，不算插件。

为什么只要一个接口（[ADR-002](https://github.com/initxy/noeta/blob/main/docs/adr/002-one-extension-interface-and-entry-points.md)）：

- **第三方插件能做的和内置插件一样多。** 27 个内置插件和外部包一样注册，这本身就证明接口够用。如果每种功能一个接口（工具一个、钩子一个、提示词一个），约定就多了，各自的能力也不均衡。
- **内核不带任何能力。** 要改内核，得有一个插件做不到的理由。
- **插件之间从不互相 import。** 它们只经由内核打交道：按模型能看到的工具名找工具（`TaskView.find_tool`），给子任务传配置（`Extension.inherit`），查这台工人能跑的代理（`TaskView.agents`），或者通过 `noeta/toolkit/plugin_records.py` 里带类型的读取函数，读另一个插件公开的记录。`lint-imports` 负责检查这一点。这样每个插件都能单独替换或关掉。
- **没有钩子能改写发给 provider 的原始请求体**，插件代码也不在进程内隔离（CPython 做不到）；隔离放在真正能隔离的地方：执行环境、MCP 服务器、命令形式的钩子。

## 钩子在什么时候被调用

<Diagram name="plug-hooks" caption="一次模型调用里的钩子，以及其余时候的钩子" />

| 钩子 | 什么时候调用 | 能做什么 |
|---|---|---|
| `on_start(view)` | 运行器每次接手任务、第一步之前 | 取只该读一次的东西（项目说明、环境信息）并记下 |
| `tools(view)` | 每一步 | 提供工具；每一步可以不一样。同名时第一个提供的插件说了算 |
| `tool_changes(view, offered)` | 每一步，所有 `tools` 之后 | 根据别的插件的工具加新工具、扣下某些工具（各带一个调用时返回的错误）、加不是工具的请求条目 |
| `context(view)` | 每次调用模型 | 系统提示里的文字（`ContextPart`），放在那一个带缓存的块里 |
| `reminders(view)` | 每次调用模型之前 | 现在记下、放进这次请求的提示（`Reminder`） |
| `before_model(view, request)` | 每次调用模型之前，提醒之后 | 返回改过的请求；抛 `TurnDone`（不调用，这一轮结束）、`RequestStale`（重新拼请求）或 `TurnFailed` |
| `on_context_overflow(view, request)` | 模型以太长为由拒了请求 | 腾出地方（压缩）并返回 `True`，然后重新调用 |
| `on_empty_reply(view, response)` | 回复里没有文字也没有工具调用 | 返回一条消息让它接着干 |
| `before_tool(view, call)` | 每个工具调用，按回复里的顺序 | `Allow`、`Deny`、`Rewrite`、`Ask` 或 `None` |
| `resolve_ask(view, call, asks)` | 某个调用马上要因为询问而挂起 | 替用户回答（分类器、钩子、回调） |
| `after_tool(view, call, output)` | 调用跑完、结果记下之前 | 改结果 |
| `on_event(view, recorded)` | 每追加一个事件之后 | 指标、审计、对外转发；配合 `delivered_through` 做到至少送达一次 |
| `reduce(state, kind, data)` | 折叠日志时 | 把本插件自己的记录折叠成状态 |
| `continue_state(state)` | 另一个任务接着这个任务的对话跑（分叉、恢复的子代理） | 决定状态里哪些带过去 |
| `inherit(state)` | 创建子任务时 | 每个子任务启动时带上的配置 |
| `before_compact`、`resident`、`lead_resident` | 压缩插件来问 | 给摘要加要求或拒绝这次压缩；交回压缩后必须留下的文字 |
| `commands(ctx)`、`run_command(ctx, name, args)` | 前端列出或执行斜杠命令 | 提供 `/name` 命令（`noeta/core/commands.py`） |

**一个调用怎么放行。** 每个插件都会被问到这一个具体的调用；有一个 `Deny` 就拒，其次是第一个 `Rewrite`，再其次才是询问（`noeta/core/runner/calls.py` 里的 `gate`）。改写后的调用会再过一遍，所以用户批准的就是真正要跑的那个调用（最多改写三次）。`Allow(approve=True)`（比如某个钩子替这个调用作保）能满足其他插件的询问，除非那个询问是 `firm` 的。调用挂起之前，各插件的 `resolve_ask` 还有机会替用户回答。一次回复里的调用按顺序过检查，遇到第一个需要询问的就停下；它后面的调用先不检查，等它有了答复再说。

## 工具

工具是任何一个带 `name`、`description`、`input_schema`、`read_only`、`clearable`，并有一个返回 `ToolOutput` 的异步 `run(input, ctx)` 的对象（`noeta/core/tool.py`）；`function_tool` 能把一个带类型标注的 Python 函数变成工具（`noeta/toolkit/function_tool.py`）。内核对工具有几条固定的处理：

- 在任何放行检查之前，先按参数说明（schema）检查输入，除非它的 flags 写了 `unchecked_input`（MCP 服务器的工具就是这样）。
- 一次回复里挨着的只读调用一起跑，最多同时十个；其他调用一个一个按顺序跑。
- 需要等待的工具返回 `ToolOutput(park=...)`：问用户问题用 `ask(...)`，等子任务用 `Park(on="children")`。这时任务不占任何工人，答复到了再调这个工具的 `resume`（见[等待与唤醒](./waiting.md)）。
- 调用在跑之前先记一笔"开始了"。崩溃之后，没跑完的调用一律答复"结果未知"，除非工具的 `recover` 能按 `ctx.idempotency_key`（`<task id>:<call id>`，在哪台工人上都一样）查到结果，或者它的 flags 写了 `retry_safe`（见[崩溃恢复](./recovery.md)）。
- 文字结果受[超长输出规则](./context.md#long-tool-outputs)约束。

其余的都在 `ToolFlags` 里：`deferred`（经 `ToolSearch` 加载）、`listed_last`、`run_last`、`past_agent_lists`、`retry_safe`、`silent_reply_after`、`unchecked_input`、`safe_while_planning`、`max_result_chars`、`search_hint`、`loaded_description`。`ToolContext` 给正在跑的工具提供执行环境、`record`、`spawn`、`post`、`keep`（让本进程里的工作期间一直占着任务的认领）、`complete`（调用模型，带重试，不算一步）、`progress`（实时输出，不记录）等等，完整列表见[参考](../reference/extension.md#toolcontext)。

## 插件状态存在日志里

<Diagram name="plug-state" caption="一条记录和这一步一起进日志，再由插件自己的 reduce 折叠" />

插件从不新增事件类型。它调用 `view.record(name, kind, data)`（工具里是 `ctx.record(...)`），运行器在钩子返回后马上把它作为 `ExtensionRecorded` 事件追加进去，和这一步的其他事件在同一次追加里，崩溃时要么都在、要么都不在。每次折叠时，插件的 `reduce(state, kind, data)` 把自己的记录变成状态，钩子用 `view.ext_state(name)`、工具用 `ctx.state[name]` 读回来。

- **为什么这样做。** 状态自动就是持久的、在每台机器上都一样：哪个工人认领了任务，就折叠同样的记录；[快照](./event-log.md)里也存着各插件的状态。插件要是把状态放在内存里，崩溃就丢，不同工人之间也会不一致。
- **`message=`。** 一条记录可以带一条消息，这条消息会进对话（项目说明、通知、提醒都是这样进去的）。
- **`folds = ("other",)`。** 插件也可以折叠别的名字下的记录，用于前端按固定名字去读的记录（计划模式的开关就记在 `permissions` 名下）。
- **删除用户消息时。** 被拦下的提问被删掉时，每个插件的 reduce 都会收到 `records.ERASED_KIND` 和被删的 seq，插件可以把为这些消息记过的东西撤回（[ADR-026](https://github.com/initxy/noeta/blob/main/docs/adr/026-a-blocked-prompt-is-erased-by-a-core-record.md)）。
- **接着跑的任务。** `continue_state` 决定分叉或恢复的子代理从什么状态开始；属于来源任务那次运行的东西（它的计时器、正在跑的后台任务）留在来源任务里。
- **内核自己也一样。** 内核的条目同样是 `ExtensionRecorded`，`extension="core"`，每种 kind 一个带类型的内容（`noeta/core/records.py`）。

在后台调用模型的插件只记一个任务编号，结果经任务的收件箱回来，再由 `on_event` 记下（[ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)；压缩插件的[提前在后台写摘要](./context.md#writing-the-summary-ahead)就是例子）。

## 插件怎么被找到

<Diagram name="plug-registry" caption="从插件代码所在的位置，到记下它的任务" />

插件和后端都按名字、通过 Python 的入口点（entry points）找到（`noeta/core/registry.py`）：

| 组 | 一个名字指向 |
|---|---|
| `noeta.plugins` | 一个 `Extension` 类（或工厂函数）；注册名就是它的 `Extension.name` |
| `noeta.providers` | `factory(**kw) -> Provider`，按 provider 类型 |
| `noeta.execenvs` | `factory(cwd, **kw) -> ExecEnv` |
| `noeta.stores` | `factory(url) -> Store`，按 URL 的协议部分 |
| `noeta.channels` | 一个 `Channel` 类 |
| `noeta.frontends` | `run(app, start)`，`noeta` 命令能打开的前端 |

内置插件在 `pyproject.toml` 里注册，和第三方包的写法完全一样；`examples/hello-plugin` 就是这样一个包，`make check` 会安装并测试它。

**插件文件。** 插件也可以只是一个 `*.py` 文件，或者一个带 `__init__.py` 的目录，放在 `~/.noeta/plugins`、受信任项目的 `.noeta/plugins`，或者只对这一次运行生效的 `--plugin-dir` 里（`registry.load_plugin_files`）。文件里定义的、带 `name` 的每个 `Extension`（或 `Channel`）子类都按这个名字注册。和已安装的包重名时，包优先。导入失败的文件会被记住原因，有人要这个插件时报出来；如果是 import 了 noeta 环境里没有的模块，会列出把它装上的那行命令。插件文件会自己加入 `add_to` 里列出的代理（`"*"` 表示所有代理）；已安装包的 `add_to` 不生效，这样装一个依赖不会悄悄改掉你的代理。

**重新加载。** `/reload-plugins`（`LocalApp.reload_plugins`）忘掉所有插件文件，从源码重新导入，再重建代理。已经在跑的那一轮用旧代码跑完；每个任务从下一轮起用新代码，并记下这一点。在服务器上，重新加载就是逐台重启工人。

**Claude Code 插件目录。** `~/.noeta/plugins`（或受信任项目的 `.noeta/plugins`）下面带 `.claude-plugin/plugin.json`、或者是 Claude Code 目录结构的目录，交给读这些格式的插件去加载（`noeta/cli/claude_plugins.py`）：`skills/` 给技能插件，`agents/` 当代理文件，`commands/` 给 `markdown_commands`，`hooks/hooks.json` 给 hooks，`.mcp.json` 给 MCP（项目里的插件除外，所以克隆下来的仓库自己不会启动任何服务器）。它的技能、代理和命令按 `<plugin>:<name>` 命名，`${CLAUDE_PLUGIN_ROOT}`、`${CLAUDE_PLUGIN_DATA}` 和它的 `userConfig` 值按 Claude Code 的方式替换。监视器、`bin/`、LSP 服务器、主题和输出风格不加载。

**安装。** `noeta plugin add <git-url>@<commit>`（也可以是一个文件，或者 Claude Code `marketplace.json` 里的一项，后者只读不执行）把插件复制进 `~/.noeta/plugins`，钉在那个提交上。安装时不执行插件里的任何东西：没有安装脚本、没有 git hooks、不装依赖。

## 插件设置

宿主程序对某个插件说的话，按名字：它的参数、一个已经建好的 `Extension`，或者开还是关。每一层都是同一种表（`build_agents`、`default_agents`、`from_settings`、`config.toml` 的 `[plugins.<name>]`），同一个插件的参数表逐个键合并：

```toml
[plugins.compaction]          # 参数，按插件名
auto_compact_clear_results = true

[plugins.web]
enabled = false               # 所有代理都关掉

[agents.main]
add_plugins = ["my_plugin", {hooks = {}}]   # 加在代理文件自己的插件后面；"*" 表示所有代理
```

- 代理文件在 frontmatter 里列出自己的插件，写名字或 `name: {参数}`（`noeta/agents/agent_files.py`）。所有从代理文件建出来的代理都会额外带上 `tool_search` 和 `structured_output`（`ALWAYS_PLUGINS`）。内置代理默认不开 `memory`、`mcp` 和 `repetition_guard`，设了才开。
- 同名同参数的插件只建一个实例，所有用到它的代理共用。
- 密钥写成引用：`{env:NAME}` 或 `{keyring:service/user}`，只在建插件实例时才解析；插件版本记录里哈希的是这个引用，不是密钥本身。
- 机器上 `/etc/noeta/config.toml` 的 `[policy]`（`allowed_plugins`、`blocked_plugins`）优先于所有用户和项目的设置。

每个内置插件的参数见[内置插件](../reference/plugins.md)。

## 插件版本记录 {#the-plugin-pin}

任务第一次启动时，以及之后某次接手时发现插件变了，都会记下它用的插件（`records.Plugins`）。每一项是 `registry.pin(extension)`：名字；代码来自哪里（`package <dist>`、`file` 或 `class <module.qualname>`）；版本（发行包的版本号，或者文件内容的哈希，目录则是其中所有 `*.py` 的哈希）；以及参数按原样写下时的哈希。

缺少某个已记录插件的工人，绝不在缺插件的情况下跑这个任务（`noeta/core/runner/task.py` 里的 `TaskRun._plugin_missing`）：它让租约自然过期，好让有这个插件的工人接手；如果连续三次认领都这样、日志里也没有新东西，就以 `plugin_missing` 让任务失败。插件在但版本不同，会重新记一次并写日志，所以同一个任务在两台工人上跑了不同的代码，日志里看得见，不会悄无声息。

为什么这么做：不然一组插件不一致的工人，会对同一个任务发出不同的请求。与其让任务的行为取决于被哪台机器接走，不如直接、明确地失败。快照的版本也把插件算在内，所以另一套插件代码做的快照会被忽略，改为从头折叠日志。

## 钩子抛了异常怎么办 {#when-a-hook-raises}

运行器对钩子漏出来的异常的处理（`noeta/core/runner/task.py` 的 `_steps`、`calls.py`、`model.py`、`closing.py`）。`reminders`、`before_model` 里抛出的 `TurnFailed`、`TurnDone`、`RequestStale`，以及 `on_context_overflow` 里抛出的 `TurnFailed`，不算出错：它们分别让这一轮带着原因失败、结束这一轮、重新拼请求。

| 钩子 | 抛异常时 |
|---|---|
| `before_tool`、`resolve_ask` | 拒绝这个调用（`permission check failed: …`），模型看到的就是这个 |
| 工具的 `run`、`resume` | 调用结果是一个写着异常的错误；这个调用暂存的东西（记录、子任务）丢掉 |
| 工具的 `recover` | 写日志；结果算作未知（`retry_safe` 的工具会重跑） |
| `after_tool` | 写日志；结果保持这个钩子收到时的样子 |
| `on_event`、`resident`、`lead_resident`、`on_empty_reply`、`commands`、`before_compact` | 写日志并跳过：这次这个插件不参与 |
| `run_command` | 原样抛给执行命令的一方 |
| `inherit` | 从 `view.spawn` 或 `ctx.spawn` 抛给发起的钩子或工具 |
| 加载任务时的 `reduce` | 等租约过期后任务被重新认领；连续三次都这样，任务以失败结束 |
| 一步里的其他钩子：`on_start`、`tools`、`tool_changes`、`context`、`reminders`、`before_model`、`on_context_overflow`、`reduce` | 这一轮失败（`internal_error`）；连续两次失败，任务以失败结束 |

这张表背后的原则：检查本身坏了就拒绝而不是放行，旁观的坏了就跳过；一个会在每次租约都重复失败的 bug，与其让任务永远停在 `running`，不如直接结束它。
