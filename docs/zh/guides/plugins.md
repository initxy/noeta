# 写一个插件

noeta 除了"按日志跑任务"这一件事，其余能力全是插件，内置插件和你自己写的插件用的是同一套写法。本页讲怎么写、怎么发布、怎么配置、怎么测。接口的逐个方法见 [Extension 接口](../reference/extension.md)，为什么这样设计见 [插件体系](../how-it-works/plugins.md)。

**除了你配置的模型 provider，noeta 不往任何地方发数据。** 没有遥测，不上传日志，不检查更新。当然，你装的插件自己发不发请求，由插件自己决定。

## 先选一种形态

插件有三种形态。三种最后都进同一个注册表（`noeta/core/registry.py`），任务点名用到之后，跑法完全一样。

<Diagram name="guide2-plugin-shapes" />

| 形态 | 是什么 | 放在哪 | 怎么加入代理 |
|---|---|---|---|
| 插件文件 | 一个 `.py` 文件，或带 `__init__.py` 的目录，里面定义一个有 `name` 的 `Extension` 子类 | `~/.noeta/plugins/`、已信任项目的 `.noeta/plugins/`，或只对这一次运行生效的 `--plugin-dir` | 类上写 `add_to`，自己加入 |
| Python 包 | 一个发行包，在 `noeta.plugins` 这个 entry point 组里登记自己的类 | 和 noeta 装在同一个环境 | 只有代理文件或 `config.toml` 点了它的名才加入 |
| Claude Code 插件 | 带 `.claude-plugin/plugin.json`、`skills/`、`agents/`、`commands/`、`hooks/hooks.json`、`.mcp.json` 的目录 | `~/.noeta/plugins/`（或已信任项目的 `.noeta/plugins/`） | 各部分交给读这些格式的插件去加载 |

先从单文件写起。等它需要自己的依赖，或者要在很多机器上共用，再打成包。

不管哪种形态，一个插件就是一个名字、一个开关，最多带这几部分：它的 `Extension`（后端：工具、提示词、关卡、状态），每个前端都会显示的界面数据（`Extension.ui`：状态项、小部件、提示），以及给网页版和桌面版用的[网页部分](#网页部分)；Claude Code 插件带的技能、代理、命令、hook 和 MCP 服务器也都算它的部分。在终端界面里跑的代码现在还不算一个部分，终端里只显示插件的界面数据。关掉一个插件，它的所有部分一起拿掉（见[关掉插件](../reference/plugins.md#switching-a-plugin-off)）。

## 插件文件：两步

1. 写 `~/.noeta/plugins/<名字>.py`，定义一个有 `name` 的 `Extension` 子类。写上 `add_to = ("main",)`（或者 `("*",)` 表示所有代理），它就会自己加入这些代理。
2. 启动一个前端；已经开着的，输入 `/reload-plugins`。

下面这个文件给模型加了一个工具：

```python
# ~/.noeta/plugins/word_count.py
from typing import Annotated

from noeta import Extension, ToolContext, function_tool


class WordCount(Extension):
    """``timeout``: seconds the count may take."""

    name = "word_count"
    add_to = ("main",)

    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout

        @function_tool(name="WordCount", read_only=True)
        async def count(ctx: ToolContext,
                        path: Annotated[str, "a text file, relative to the working directory"]) -> str:
            """Count the words in a text file."""
            res = await ctx.env.run(["wc", "-w", path], timeout=self.timeout)
            return (res.stdout + res.stderr).decode(errors="replace")

        self._tool = count

    def tools(self, view):
        return [self._tool]
```

`function_tool`（`noeta/toolkit/function_tool.py`）把一个带类型标注的函数变成工具：工具名取函数名（或 `name=`），说明取 docstring，参数结构从标注生成（支持 `str`、`int`、`float`、`bool`、`list[...]`、`dict`、`Literal`、`X | None`；`Annotated[T, "说明"]` 给参数加说明）。标注为 `ToolContext`（或参数名叫 `ctx`）的参数拿到这次调用的上下文，不进参数结构。同步函数在线程里跑，不能调 `ctx.record`、`ctx.post`、`ctx.keep`，要用它们就写成 async。

命令通过 `ctx.env` 跑，也就是任务的执行环境：本机、沙箱或容器，任务的命令在哪跑它就在哪跑（见[命令在哪里执行](./sandbox.md)）。凡是涉及任务文件的事，不要在工人进程里直接 `subprocess`：任务的下一步可能在另一台机器上跑。

想把工具写成类，给它 `name`、`description`、`input_schema`、`read_only`、`clearable` 和 `async def run(self, input, ctx) -> ToolOutput`（`noeta/core/tool.py`）即可，[`examples/hello-plugin`](https://github.com/initxy/noeta/blob/main/examples/hello-plugin/src/noeta_hello/__init__.py) 就是这么写的。

放在别处的文件，只想试一次：`noeta --plugin-dir ./word_count.py`（`-p` 打印模式下也能用；也可以给一个插件目录或 Claude Code 插件，参数可以重复）。它只加载进当前进程，所以和 `--server` 一起用会被拒绝。没加入任何代理的插件文件会有一条提示，告诉你去设 `add_to` 或 `add_plugins`。

`noeta plugins` 列出所有插件和各自来源。导入失败的文件会标成坏的并写明原因；如果是缺了 noeta 环境里没有的模块，还会给出补装的那行 `uv tool install --with …` / `pip install` 命令（插件自己不会装任何东西）。

## 配置

插件构造函数的关键字参数就是它的配置项，写在 `~/.noeta/config.toml` 里：

```toml
[plugins.word_count]        # 按插件名写构造参数
timeout = 60

[plugins.web]
enabled = false             # 对所有代理关掉（每个插件都有这个开关）

[agents.main]
add_plugins = ["word_count", {hooks = {}}]   # 接在代理文件自己的插件后面

[agents."*"]                # 所有代理
add_plugins = []
```

也可以写在代理文件的 `plugins` 列表里，按顺序列插件，每项是一个名字或 `名字: {参数}`：

```yaml
plugins:
  - word_count: {timeout: 60}
```

同一个插件的参数表在各层之间按键合并（`build_agents`、`default_agents`、`from_settings`、`config.toml`）。全部配置项见[配置项](../reference/config.md)。`enabled = false` 是彻底关掉；前端还可以只在一个任务里关，或者让以后的新任务都不带它（见[关掉插件](../reference/plugins.md#switching-a-plugin-off)）。

密钥写成引用：`"{env:NAME}"` 或 `"{keyring:service/user}"`，只在构造插件的那一刻才解析（`noeta/agents/agent_files.py` 的 `resolve_secrets`）。这样点名的环境变量，不会传给 noeta 启动的任何命令、hook 或 MCP 服务器。

## 代码能挂在哪里

`Extension`（`noeta/core/extension.py`）的每个方法都可以不写，用到哪个写哪个。

| 你想…… | 写什么 | 代码位置 |
|---|---|---|
| 给模型一个工具 | `Extension.tools()` 返回 `Tool`，或对带类型的函数用 `function_tool(fn)` | `noeta/core/tool.py`、`noeta/toolkit/function_tool.py` |
| 基于其他插件的工具再造一个工具，或把某些工具从请求里拿掉 | `Extension.tool_changes(view, offered)` → `ToolChanges`（`noeta/plugins/tool_search` 就是这样处理延迟加载工具的） | `noeta/core/extension.py` |
| 往系统提示词里加文字 | `Extension.context()` → `ContextPart`（放在那一整块缓存的系统提示里） | `noeta/core/extension.py` |
| 每轮告诉模型一些事 | `Extension.reminders()` → `Reminder`（会记进日志，任务恢复后照样发） | `noeta/core/extension.py` |
| 放行、拒绝、改写或询问某次工具调用 | `before_tool` → `Allow` / `Deny` / `Rewrite` / `Ask`；`resolve_ask` 回答询问；`after_tool` 改结果 | `noeta/core/extension.py` |
| 用 Claude Code 的格式跑 shell / HTTP hook | settings 里的 `hooks`，或 Claude Code 插件的 `hooks/hooks.json`（含 InstructionsLoaded） | `noeta/plugins/hooks` |
| 加斜杠命令 | `commands()` / `run_command()`，或 Claude Code 插件里的 `commands/*.md` | `noeta/core/commands.py` |
| 状态要扛得住重启、跨机器 | `view.record(name, kind, data)` + `reduce()`（绝不新增事件类型） | `noeta/core/extension.py` |
| 读别的名字下写的记录（前端按名字读的那些） | `folds = ("other",)`：这些记录也会进你的 `reduce` | `noeta/core/extension.py` |
| 过一段时间再叫醒任务 | `view.set_timer(name, at, text)` | `noeta/core/records.py` |
| 问用户一个问题 | `ask(...)`（调用停下来等，期间任务不占工人） | `noeta/core/questions.py` |
| 加代理或子代理 | 一个代理 markdown 文件（Claude Code 的格式，外加 `plugins`） | `noeta/agents/agent_files.py` |
| 加技能 | `~/.noeta/skills`、`~/.agents/skills`、项目的 `.noeta/skills` 或 `.agents/skills`、插件的 `skills/` 下的 `SKILL.md` 目录；`~/.noeta/commands` 或项目 `.noeta/commands` 下的 `*.md` | `noeta/plugins/skills` |
| 接 MCP 服务器 | `.noeta/mcp.json`、settings 的 `mcpServers`、插件的 `.mcp.json` | `noeta/plugins/mcp` |
| 让聊天软件或 webhook 进来 | 一个渠道 `Channel`（`run(client, stop)`），用 `deliver()` 投递 | `noeta/core/channel.py` |
| 把回复送出去 | 任务里某个 `Extension` 的 `on_event` | [`chat-bridge`](https://github.com/initxy/noeta-plugins/tree/main/chat-bridge) |
| 接一种新的模型 API | `noeta.providers` 组里的一个 provider；写一个 `family = "<它的名字>"` 的 `[providers.<name>]` 表，就能和其他 provider 并存、按模型 id 分流 | `noeta/backends/providers`、`noeta/backends/providers/routing.py` |
| 让命令在别处跑（容器） | `noeta.execenvs` 组里的一个执行环境 | `noeta/backends/execenvs` |
| 把任务存到别处 | `noeta.stores` 组里的一个存储，用 `noeta.testing.store_contract` 检查 | `noeta/backends/stores` |
| 新前端或新协议 | 客户端接口（`Client`、`RemoteClient`、HTTP），不要另起一套 API | `noeta/core/client.py`、`noeta/host` |
| 修改每次模型请求，或者不调模型直接结束这一轮 | `before_model` → `ModelRequest`；抛 `TurnDone`（不调用）或 `RequestStale`（重新组请求） | `noeta/core/extension.py` |
| 任务开始时抓一次信息 | `on_start`（会记下来，任务恢复后发的内容不变） | `noeta/core/extension.py` |
| 把配置传给子代理，或决定分叉时带走什么 | `inherit`、`continue_state` | `noeta/core/extension.py` |
| 起一个子任务，或往收件箱投消息 | `view.spawn`、`view.post` / `view.enqueue` | `noeta/core/extension.py` |
| 用别的插件的工具 | `view.find_tool(name)` | `noeta/core/extension.py` |
| 在 hook 或工具里调模型 | `view.complete`、`ctx.complete`（会重试，不算一步） | `noeta/core/extension.py`、`noeta/core/tool.py` |
| 在后台让模型干活、稍后再用结果 | 从 hook 里启动，结果作为 `External` 投回收件箱，在 `on_event` 里记下（[ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)）；两轮之间的活用 `TaskState.model_called_at` 和 `view.next_request`（`noeta/plugins/compaction` 就是这么做的） | `noeta/core/extension.py` |
| 显示调用进度 | `ctx.progress(text)`：只实时显示，不记日志 | `noeta/core/tool.py` |
| 调用到一半进程崩了也能收拾 | `Tool.recover`（按 `ctx.idempotency_key` 查结果）或 `flags = ToolFlags(retry_safe=True)`；只活在一个进程里的东西用 `ctx.keep()` | `noeta/core/tool.py` |
| 工具等模型要用时才发给它 | `flags = ToolFlags(deferred=True)`（经 `ToolSearch` 加载） | `noeta/plugins/tool_search` |
| 文字在压缩后保留，或者参与压缩 | `resident` / `lead_resident`；`before_compact`；`on_context_overflow(view, request)` | `noeta/core/extension.py` |
| 把结果以数据形式给前端展示 | `ToolOutput.display` → `Display(kind, data, fallback)` | `noeta/core/tool.py`、`noeta/core/types.py` |
| 在每个前端里显示状态项、小部件或提示 | `ui(view)` → `StatusItem` / `Widget` / `Notice`，从折叠出来的状态算 | `noeta/core/extension.py` |
| 往网页版和桌面版里放自己的组件 | 一个[网页部分](#网页部分)，通过 `ui_call` 回调自己的插件 | `web/sdk` |
| 模型回了个既没文字也没工具调用的空回复时继续（否则这一轮就结束了） | `on_empty_reply(view, response)` → 用来继续的那条消息，或 `None`（参考 `noeta/plugins/empty_reply.py`） | `noeta/core/extension.py` |
| 拦下一条用户输入，或让一轮继续下去（就像 UserPromptSubmit 和 Stop hook） | `reminders` + `view.erase_messages` + 抛 `TurnFailed`；在 `on_event` 里看到 `ModelReplied` 就投一条 `Say(when="now")` | `noeta/plugins/hooks/extension.py` |

插件要用的名字都能从顶层包导入：`from noeta import Extension, Tool, ToolContext, ToolOutput, ToolUse, Allow, Deny, Ask, Rewrite, Reminder, ContextPart, Command, CommandContext, Recorded, ToolChanges, TaskState, TaskView, ask, read_answer, function_tool, text_output`（`noeta/__init__.py`）。

## 扛得住崩溃的状态

插件不在自己的属性里存任务状态：任务的下一步可能在另一个进程里跑。它把发生的事记进日志，需要时再从日志算回来。

```python
class Greeter(Extension):
    name = "greeter"

    def initial_state(self):
        return {"greeted": []}

    def reduce(self, state, kind, data):          # 纯函数：同样的日志得出同样的状态
        if kind == "greeted":
            return {**state, "greeted": [*state["greeted"], data["name"]]}
        return state

    async def after_tool(self, view, call, out):
        if call.name == "Greet" and not out.is_error:
            view.record(self.name, "greeted", {"name": call.input.get("name", "")})
        return out
```

`view.record(name, kind, data)` 追加一条 `ExtensionRecorded` 事件；任务每次被认领时，都会把这些记录依次喂给 `reduce`，`view.ext_state(name)` 读出结果。工具里用 `ctx.record(...)` 也一样，只是调用失败时这些记录会被丢掉。记录是唯一的状态来源：插件不会新增事件类型。所以崩溃、恢复、分叉时你的插件和内置插件的表现完全一致（见[任务就是一条日志](../how-it-works/event-log.md)）。

插件能做的其他"持久"的事，也都走日志或收件箱：

- **问用户。** 工具返回 `ToolOutput(park=ask({...}))`；任务停下来等，不占工人，拿到回答后调 `resume(input, ctx, outcome)`（用 `read_answer` 读答案）。所有前端都能显示这个问题，你不用写任何前端代码。`examples/hello-plugin` 有示范。
- **告诉用户。** `view.notify(text)` 显示一条通知，模型看不到。
- **稍后叫醒任务。** `view.set_timer(name, at, text)` 是一条记录，到点时由存储在持有这个任务的那个工人上触发。
- **只抓一次。** 在 `on_start` 里读取并记下来，先查一下是不是已经记过。这样任务恢复后，即便源文件变了，发给模型的字节也不变（`examples/house-style`）。
- **有名字的文案。** `view.prompt("<插件>/<键>", 默认值)` 返回用户可以按这个名字覆盖的文字（`noeta/core/prompts.py`）；覆盖内容在任务开始时记下来。

## hook 抛异常时会怎样

hook 漏出来的异常，执行器怎么处理（`noeta/core/runner/task.py` 的 `_steps`、`calls.py`、`model.py`、`closing.py`）。`reminders`、`before_model`、`on_context_overflow` 抛出的 `TurnFailed`、`TurnDone`、`RequestStale` 不算出错：它们分别是带原因地让这一轮失败、结束这一轮、重新组请求。

| hook | 抛异常时 |
|---|---|
| `before_tool`、`resolve_ask` | 这次调用被拒绝（`permission check failed: …`），模型看到的就是这个 |
| 工具的 `run`、`resume` | 调用结果是一个写明异常的错误；这次调用暂存的东西（记录、子任务）丢弃 |
| 工具的 `recover` | 写日志；结果按"未知"处理（`retry_safe` 的工具会重跑） |
| `after_tool` | 写日志；结果保持这个 hook 拿到时的样子 |
| `on_event`、`resident`、`lead_resident`、`on_empty_reply`、`commands`、`before_compact` | 写日志并跳过：这一次这个插件不参与 |
| `run_command` | 抛给执行命令的一方（前端，或展开这个命令的 hook） |
| `inherit` | 从 `view.spawn` 抛出，交给发起子任务的工具或 hook |
| 加载任务时的 `reduce` | 等租约过期后任务被重新认领；连续三次都这样，任务以失败结束 |
| 一步里其他任何地方：`on_start`、`tools`、`tool_changes`、`context`、`reminders`、`before_model`、`on_context_overflow`、`reduce` | 这一轮失败（`internal_error`）；连续第二次失败，任务以失败结束 |

## 打成 Python 包

在 `pyproject.toml` 里登记这个类，然后和 noeta 装在同一个环境里。内置插件就是这么做的（仓库根目录的 `pyproject.toml` 列了全部 27 个）。

```toml
[project.entry-points."noeta.plugins"]
hello = "noeta_hello:Hello"
```

其他几组用法相同：`noeta.channels`、`noeta.providers`、`noeta.execenvs`、`noeta.stores`、`noeta.frontends`。[`examples/hello-plugin`](https://github.com/initxy/noeta/tree/main/examples/hello-plugin) 是一个完整的例子：一个 `Greet` 工具、从日志算出来的状态、一个问用户的问题、一条带通知的提醒、一个 `/greet` 命令。

装好的包和插件文件同名时，包优先；包的 `add_to` 会被忽略：装一个依赖，不会悄悄改变你的代理。想让它在哪里生效就在哪里点名，代理文件里写 `- hello: {greeting: Hi}`，或 `config.toml` 里写 `[agents.main] add_plugins = ["hello"]`。

用 `uv tool install` 装的 noeta，用 `--with` 把包装进同一个环境：

```sh
uv tool install --reinstall --with ./noeta-hello 'noeta[tui]'
```

重装会丢掉上次装时带的 `--with` 和 extras，所以要全部重新写上。

## Claude Code 插件

把 Claude Code 插件目录（`.claude-plugin/plugin.json`、`skills/`、`agents/`、`commands/`、`hooks/hooks.json`、`.mcp.json`）放进 `~/.noeta/plugins/`，各部分会交给读这些格式的插件去加载（`noeta/cli/claude_plugins.py`）：

| 部分 | 由谁加载 | 名字 |
|---|---|---|
| `skills/` | `skills` 插件 | `<插件>:<技能>` |
| `agents/**/*.md` | 代理文件读取器 | `<插件>:<代理>` |
| `commands/**/*.md` | `markdown_commands`（用户输入时）和 `skills`（模型调用时） | `<插件>:<命令>` |
| `hooks/hooks.json` | `hooks` 插件 | — |
| `.mcp.json` | `mcp` 插件；项目里的插件目录永远不加载它 | 服务器别名 |
| `settings.json` 的 `agent`（没有就看 manifest 的 `settings`） | 前端启动时用的代理，优先级低于你自己的设置 | — |

子目录会在名字前加 `<目录>:`，和 Claude Code 一样。命令和技能一样，`!` 开头的行要过权限检查，`allowed-tools` 里列的工具会被预先放行。`${CLAUDE_PLUGIN_ROOT}` 和 `${CLAUDE_PLUGIN_DATA}`（`~/.noeta/plugin-data/<name>`）会在 hook、MCP 服务器、技能、命令和代理提示词里替换。

manifest 里的 `userConfig` 在 `noeta plugin add` 时逐项询问（或用 `--config KEY=VALUE` 直接给；`noeta plugin configure <name>` 可以重新填）。值存在 `~/.noeta/settings.json` 的 `pluginConfigs.<插件>.options`，敏感值放进系统钥匙串（服务名 `noeta-plugin`，账户 `<插件>/<键>`），文件里只留一个 `{keyring:…}` 引用（`noeta/cli/plugin_options.py`）。替换规则和 Claude Code 一致：MCP 服务器配置和 hook 命令里的 `${user_config.KEY}` 能用所有值（shell 形式的 hook 命令不能用，它的 `args` 可以），命令和代理提示词里只能用非敏感值；每个 hook 都能通过 `CLAUDE_PLUGIN_OPTION_<KEY>` 拿到所有值。

某台机器读不到的密钥，只影响用到它的东西，并给出通知：hook 拿不到这个变量；点名它的 MCP 服务器或 hook 不加载；点名它的 `PreToolUse` / `PermissionRequest` / `UserPromptSubmit` hook 直接拒绝。插件自己的文件只能引用它自己的钥匙串密钥。

项目 `.noeta/plugins/` 里的插件，其 MCP 服务器永远不会启动：noeta 会提示你把它们抄进 `.noeta/mcp.json`，那里需要你明确同意（见 [MCP 服务器](./mcp.md)）。监视器（monitors）、`bin/`、LSP 服务器、主题和输出风格不加载（原因见[和其他 agent 对比](../operations/comparison.md#逐项对比)）。noeta 从不读 `~/.claude` 或 `.claude/`。

Python 插件目录（带 `__init__.py` 的）也可以在代码旁边放 `skills/`、`commands/`、`agents/`，加载方式相同。

## 安装

`noeta plugin`（`noeta/cli/plugin_command.py`）把插件复制进 `~/.noeta/plugins/`，固定在某个版本，并把来源记在 `~/.noeta/plugin-sources.json`。

```sh
noeta plugin add https://github.com/you/your-plugin@<完整提交号>   # https、ssh、user@host:path 或 file://
noeta plugin add ./my_plugin.py                                    # 文件或目录，复制过去
noeta plugin add repetition_guard                                  # 只写名字，从配好的插件市场里找
noeta plugin add --marketplace ./team-marketplace formatter        # Claude Code 的 marketplace.json
noeta plugin add --marketplace https://github.com/team/market@<提交号> formatter --commit <sha>
noeta plugin configure formatter --config API_URL=https://example.com
noeta plugin list
noeta plugin remove your-plugin
```

<Diagram name="guide2-plugin-install" />

- **git 来源必须给完整提交号**（40 位十六进制；SHA-256 仓库是 64 位）。短提交号会被拒：以后推上来的提交可能和它前缀相同。明文的 `http://` 和 `git://` 也会被拒：中间人可以换掉代码。
- **安装时插件里的东西一行都不执行**：clone 时设 `core.hooksPath=/dev/null`，不拉子模块，不跑安装脚本，不装依赖；检出后删掉 `.git`。插件如果导入了 noeta 环境里没有的包，会被列为坏的（`noeta plugins`、`/reload-plugins`），并给出安装那个包的命令。
- **marketplace 只读**，只用来找插件的来源。从仓库取的条目装在它的 `sha` 上；条目没固定版本时，用 `--commit <sha>`。只写了 `ref`（分支或标签）的条目照样被拒，算作没固定提交，报错里会点出这个 ref：分支和标签会变，noeta 只装固定下来的提交。写成路径的条目靠 marketplace 本身固定：marketplace 在本地磁盘上，或者是写了 `<url>@<提交号>` 的仓库，都可以；仓库按默认分支读的话，这种条目会被拒。支持 `github`、`url`、`git-subdir` 和路径这几种来源。
- **带 `userConfig` 的插件**在安装时一起配置。配置失败，插件不会以半配置状态留在那里；对不声明任何选项的插件传 `--config`，也会拒绝安装。
- 插件名取文件名（去掉后缀），否则取 manifest 的 `name`，再否则取 marketplace 里给它的名字，最后才取目录名。`~/.noeta/plugins/` 里已有同名的，要先 `noeta plugin remove`。

已经开着的前端，用 `/reload-plugins` 载入新装或删掉的插件。

### 插件市场

插件市场就是 Claude Code 的 `.claude-plugin/marketplace.json`。`noeta plugin add <名字>` 会去 `~/.noeta/marketplaces.json` 里配好的市场挨个找这个名字。官方市场 [`initxy/noeta-plugins`](https://github.com/initxy/noeta-plugins) 默认就配着，不想要可以删掉；不属于 noeta 本体的可选插件都在那里（`repetition_guard`、`chat-bridge`、`chat-approvals`）。

```sh
noeta plugin marketplace add ./team-marketplace                  # 本地目录
noeta plugin marketplace add https://github.com/team/market       # 按默认分支读
noeta plugin marketplace add https://github.com/team/market@<提交号>
noeta plugin marketplace list                                     # 每个市场和它列出的插件
noeta plugin marketplace remove team
```

- 按默认分支读的市场只当数据用：清单可以变，但每个条目照样只装它固定的那个提交，所以你不重新安装，装好的东西就不会变。清单读一次缓存一小时（`~/.noeta/marketplace-cache/`），`add` 时会重新读。
- 市场的名字取它 `marketplace.json` 里的 `name`。加一个和已有市场同名的，就是把那个换掉（比如把 `noeta-plugins` 指到本地的一份检出）。
- 两个市场都列了同一个名字时，用 `noeta plugin add <名字> --marketplace <其中一个>` 指定。
- 某个市场读不出来，会说清是哪个，其余的照常可用。
- 条目里可以多写一个给 noeta 前端看的 `noeta` 对象，比如 `{"ui_api": 1, "parts": ["backend", "web"]}`；Claude Code 不认它，也不受影响。

### 机器级策略

`/etc/noeta/config.toml`（机器的托管目录；macOS 上是 `/Library/Application Support/Noeta`，Windows 上是 `C:\Program Files\Noeta`）可以按名字允许或屏蔽插件，优先级高于所有用户和项目设置（`noeta/cli/plugin_policy.py`）：

```toml
[policy]
allowed_plugins = ["word_count"]   # 设了以后，非 noeta 自带的插件只加载这几个
blocked_plugins = ["web"]          # 永不加载，自带的也一样
```

插件文件在导入之前就按文件名检查；所有插件、渠道和 Claude Code 插件按名字检查。

## 测试

`noeta.testing` 提供了一个按剧本回答的假模型：`ScriptedProvider` 每次被调用就按顺序给出你预先写好的下一步，并把每个请求都存下来；`reply(text, *calls)` 用来造一条回复。配上临时目录里的 sqlite 存储，就能在测试进程里完整跑一个任务：

```python
from pathlib import Path

from noeta import Agent, Runtime
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.testing import ScriptedProvider, reply

PLUGIN_DIR = Path(__file__).parent / "plugins"     # word_count.py 所在的目录


async def test_word_count(tmp_path):                 # pytest-asyncio，asyncio_mode = "auto"
    (tmp_path / "a.txt").write_text("one two three\n")
    registry.load_plugin_files([PLUGIN_DIR])
    plugin = registry.load(registry.PLUGINS, "word_count")()
    provider = ScriptedProvider([reply("", ("WordCount", {"path": "a.txt"})), reply("done")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": Agent(name="main", model="m", extensions=(plugin,))},
                 provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    client = Client(store)
    task = await client.create("main", "count the words in a.txt")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(task)
                if isinstance(r.event, ToolFinished)]
    assert "3" in result.text
    assert len(provider.requests) == 2               # 每次发给模型的请求都在这里
    await store.close()
```

`provider.requests` 存着每一个 `ModelRequest`，测试可以精确检查某条提醒或某段上下文到底给模型放了什么。`examples/run-tests/tests/test_run_tests.py` 检查了一条提醒只发一次、第二轮不会重复。`make example` 跑所有示例的测试。

存储后端要跑共用的契约测试：定义一个 `store` fixture，每次给一个新的空存储，然后在旁边写 `from noeta.testing.store_contract import *`。

## 持久与多机

照上面规则写的插件，本来就能在多台机器上跑。内核另外加了这几条规则：

- **任务固定自己的插件集。** 任务开始时记下它用的插件（名字、包版本或文件哈希、参数哈希；`registry.pin`）。缺其中某个插件的工人，绝不会在缺插件的情况下跑这个任务：它让租约自然过期，好让有这个插件的工人接手；同一个工人连续三次这样认领、日志里又没有任何新内容，就以 `plugin_missing` 让任务失败（次数记在这个工人的内存里，重启后从头算）。插件集变了会重新记录。
- **重新加载。** `/reload-plugins` 重新导入插件文件、重读设置文件、重建代理。正在进行的一轮用旧代码跑完；每个任务从下一轮开始用新代码。服务端的重新加载就是滚动重启：一次停一个 `noeta worker`（它手上在跑的任务回到存储，由别的工人按日志接着跑），装好，再启动。要在几个租约周期内做完：固定了新插件的任务如果一直落到没有这个插件的工人上，三个租约后就会失败。同一个任务在两个工人上跑了不同代码，日志里能看到，不会悄无声息。
- **渠道**在每个启用它的 `noeta worker` / `noeta serve` 里运行；外部投递进来的消息是幂等的，所以起多个副本也安全，回复则只从任务唯一的写入者发出去（见[作为服务端运行](./server.md)）。
- **事件投递。** `on_event` 在事件提交后收到每个事件一次；它还没跑进程就崩了，这个事件就丢了。如果插件记下自己处理到哪个事件，并在 `delivered_through(view)` 里返回那个序号，就能保证至少收到一次：任务每次被认领，都会先把那个序号之后的所有事件（连同任务当前状态）再给它一遍。任务进入 `Waiting` 交还之前，会等这个任务的所有 `on_event` 跑完、记录落盘，所以中间崩溃的话，任务会在租约过期后被重新认领，等待也不会被写两次。失败的副作用（比如一次投递抛了异常）在下一次认领时重试；对正在等待的任务来说，就是它收件箱里来下一条消息的时候。唯一的例外是结束任务的那条 `TaskEnded`：已结束的任务不会再被认领，所以它的 `on_event` 只跑一次，期间写的记录会被丢掉。副作用要按事件序号做幂等（参考 chat-bridge 的 `Idempotency-Key`）。
- **只活在一个进程里的东西**（一个子进程、一条打开的连接），用 `ctx.keep()` 让任务一直留在这个工人上；工人挂了，下一个认领者会告诉模型这份工作丢了（见[单写者与崩溃恢复](../how-it-works/recovery.md)）。
- **策略。** `/etc/noeta/config.toml` 的 `[policy]` 优先于所有用户和项目设置（见上文）。

## 网页部分

插件目录（或 Python 包）还可以带一段给网页版和桌面版用的代码：一个构建好的 ES 模块，由插件代码旁边的 `.noeta-plugin/plugin.json` 指明。

```json
{ "name": "my-plugin", "version": "0.1.0", "ui_api": 1, "web": "web/dist/index.js" }
```

模块导出 `activate(ctx)`，把 React 组件放进页面预留的位置（slot）：

```tsx
import { SLOTS, type PluginContext } from "@initxy/web-sdk";

export function activate(ctx: PluginContext) {
  return ctx.slots.add(SLOTS.sidebar, { key: "files", mount: "keyed" }, () => (
    <button onClick={() => ctx.call("workspace")}>Look</button>
  ));
}
```

`ctx.call(name, args)` 调到插件自己后端的 `Extension.ui_call`；`ctx.client` 操作当前打开的会话对应的任务，`ctx.records(kind)` 和 `ctx.allRecords()` 读插件自己的记录（组件里用 `useRecords`/`useAllRecords`，记录有新的就跟着刷新），`<ctx.Transcript taskId=…/>` 显示另一个任务的对话，比如某个子代理的。内置插件自己的界面（noeta 仓库里的 `web/src/plugins/<name>/`）也是这样写的。构建成一个 ES 模块，`react`、`react-dom`、`react/jsx-runtime`、`@initxy/web-sdk`、`@initxy/ui` 不打进包里（页面自己提供，用的是页面那一份；构建脚本见 `web/sdk/template`），并把构建产物提交进仓库：安装插件时不会跑任何构建。`ctx` 的每个成员、所有位置和挂载方式见 [`web/sdk` 的 README](https://github.com/initxy/noeta/blob/main/web/sdk/README.md)；[`examples/web-panel`](https://github.com/initxy/noeta/tree/main/examples/web-panel) 是一个带网页部分的完整插件。每个位置在页面上的哪里、页面拿到网页部分后怎么处理，见[网页、桌面与编辑器](./front-ends.md#网页版和桌面版里的插件)。

## 替换内置插件

把内置插件关掉（`[plugins.<name>] enabled = false`），加上你自己的。网页版里内置插件的界面就是它的网页部分，会跟着一起关掉，替换的插件自己带界面。终端界面和编辑器（ACP）会按名字读几个插件的记录，替换这几个时要保留原来的名字和记录：`permissions`（`mode`、`modes`：前端显示和切换的模式）和 `checkpoints`（`snapshot`：回退）。`plan_mode` 把它的切换写成 `permissions` 名下的 `mode` / `plan_exit` 记录并读回来（`folds`），所以关掉权限插件计划模式也能用（这时除了写计划文件，凡是既不只读、也不是 `safe_while_planning` 的调用都会询问）；而关掉 `plan_mode` 后，权限插件不提供 `plan` 模式。因此替换 `permissions` 的插件，绝不能用别的含义去写这些记录类型：`mode`、`plan_exit`、`modes`（前端、`plan_mode` 在读），`plan_name`、`noted`、`compacted`、`plan_file`、`plan_file_sent`（`plan_mode`，拆分之前的日志里有），`step`、`tool_call`、`cost`（`budget`，同理）。

谁读哪条记录，统一登记在 `noeta/toolkit/plugin_records.py`。

## 现成的例子

每个都是一个文件（hello-plugin 是一个包，web-panel 是一个目录）、一份简短 README，外加一个 `make example` 会跑的测试：

- [`examples/run-tests`](https://github.com/initxy/noeta/tree/main/examples/run-tests)：一个工具。在任务跑命令的地方执行测试命令，从日志里算出上次结果，改过文件后提醒模型一次。配置 `[plugins.run_tests] command = "uv run pytest -q"`。
- [`examples/house-style`](https://github.com/initxy/noeta/tree/main/examples/house-style)：提示词和提醒。任务开始时抓一次输出风格，任务恢复后发的字节不变；加入所有代理（`add_to = ("*",)`）。
- [`examples/hello-plugin`](https://github.com/initxy/noeta/tree/main/examples/hello-plugin)：同类插件的 Python 包版本，带 entry point。
- [`examples/web-panel`](https://github.com/initxy/noeta/tree/main/examples/web-panel)：带网页部分的插件目录。一个状态项，外加侧栏里一块会调用自己后端的面板。

官方市场里的插件也可以当例子看，每个都是一个插件目录，带自己的测试：[`chat-bridge`](https://github.com/initxy/noeta-plugins/tree/main/chat-bridge) 是一个渠道，聊天里的一个话题对应一个任务，请求带共享密钥，发送者按名单放行，回复从任务里转发出去；[`chat-approvals`](https://github.com/initxy/noeta-plugins/tree/main/chat-approvals) 是和它配套的第二个渠道，任务在等的审批会发到对应话题里，聊天软件里点"允许"或"拒绝"就是回答；[`repetition_guard`](https://github.com/initxy/noeta-plugins/tree/main/repetition_guard) 是一道 `before_tool` 关卡。

内置插件和它们的配置项列在[内置插件](../reference/plugins.md)。
