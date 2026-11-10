# noeta 命令

`noeta` 的每个子命令和参数，以及终端界面里的斜杠命令。内容来自 `noeta/cli/main.py`、
`noeta/cli/headless.py`、`noeta/cli/service_command.py`、`noeta/cli/update_command.py`、
`noeta/cli/plugin_command.py` 和 `noeta/cli/mcp_command.py`。想按使用场景
了解，看[终端界面与 -p](../guides/terminal.md)。

<Diagram name="ref-ways-in" caption="前端要么在自己进程里的 LocalApp 上跑（只管自己的任务），要么通过 RemoteApp 连服务端；serve 和 worker 运行存储里的所有任务。" />

## 总览

```text
noeta [参数] [提示词]           终端界面；带 -p、--output-format 或 --input-format stream-json 就是无界面模式
noeta acp [--server URL]        在标准输入输出上提供 Agent Client Protocol，给编辑器用
noeta serve [参数]              HTTP / WebSocket 接口，默认同时跑 worker（--no-worker 关掉）
noeta service install|status|logs|start|stop|restart|uninstall
                                把 noeta serve 装成当前用户的系统服务（systemd、launchd）
noeta update                    升级到最新版，装时带的 extra 都保留
noeta worker [参数]             从存储里认领并运行任务，直到被停止
noeta trust                     信任当前目录
noeta plugins                   列出所有插件及其来源
noeta plugin add|configure|list|remove|marketplace
noeta mcp add|add-json|list|get|remove|login|logout
```

第一个不是参数的词如果是子命令名，就按子命令处理。子命令前面只能出现下面的通用参数（`--model`、
`--effort`、`--permission-mode`、`--provider`、`--base-url`、`--store`、`--cwd`、
`--system-prompt`、`--append-system-prompt`）；出现任何别的参数，都按终端界面或 `-p` 处理。

## 通用参数

终端界面、`-p`、`acp`、`serve`、`web`、`worker` 都接受。

| 参数 | 含义 |
|---|---|
| `--model MODEL` | 模型 id 或别名（`opus`、`sonnet`、`haiku`、`fable`）；不给就依次看 `NOETA_MODEL`、`config.toml`、设置文件里的 `model` |
| `--effort {low,medium,high,xhigh,max}` | 模型思考的力度；不给就看 `NOETA_EFFORT`、`config.toml` 的 `effort` |
| `--permission-mode MODE` | 起始权限模式：`default`、`acceptEdits`、`plan`、`auto`、`dontAsk`、`bypassPermissions`，或 `manual`（`default` 的别名）。默认 `bypassPermissions`（关掉了 bypass 模式的地方是 `auto`），或设置文件里的 `permissions.defaultMode` |
| `--provider {anthropic,openai_compat,openai_responses}` | 默认 provider 的类型；不给就看 `NOETA_PROVIDER`、`config.toml` 的 `provider`（后者也可以是某个 `[providers.<name>]` 表的名字） |
| `--base-url URL` | 默认 provider 的地址；不给就看 `NOETA_BASE_URL` |
| `--store URL` | `sqlite:///path` 或 `postgres://…`；不给就看 `NOETA_STORE`、`config.toml` 的 `store`，最后是 `~/.noeta/tasks.db` |
| `--cwd PATH` | 工作目录（默认当前目录）。配合 `--server` 时是服务端上的目录（默认服务端自己的） |
| `--system-prompt TEXT` | 替换 main 代理的系统提示词（提示词名 `system/main`） |
| `--append-system-prompt TEXT` | 在 main 代理的系统提示词后面追加文字（`system/main.append`） |

哪里都没指定模型会报错（`noeta: no model configured`），只有用 `--server` 连服务端的前端例外，
它用服务端的模型。

`--permission-mode` 和 `--effort`（连同 `NOETA_EFFORT`、`config.toml` 的 `effort`）成为各个进程
新建任务时的起始配置；任务一直带着创建时的配置，之后不管哪个进程来跑都一样：

- 终端界面和 `-p`：它们开的任务；
- `acp`：编辑器每开一个会话对应的新任务（带 `--server` 也一样，这时由服务端的权限插件判断这个模式）；
- `serve`：客户端创建的每个任务都以此为默认，`--no-worker` 时也一样；请求自己给了 `effort` 或
  `permission_mode` 的，以请求为准；
- `worker`：只管它的渠道开的任务（它跑的其他任务是别人建的，保留各自的配置）；给了 `--effort` 或
  `--permission-mode` 时，它会在 stderr 上说明这一点。

`serve` 和 `worker` 同时给了 `--permission-mode bypassPermissions` 和 `--disable-bypass-permissions`
时直接报错退出：`noeta: --permission-mode bypassPermissions contradicts --disable-bypass-permissions`。

## 终端界面：`noeta`

不带无界面参数时，`noeta` 打开终端界面（`noeta-tui` 包，通过 `noeta.frontends` entry point 找到；
没装时 noeta 会打印安装命令）。不带 `-p` 时给的提示词（`noeta "把测试修好"`）会在第一个任务打开后，
像你亲手输入的一样作为第一条消息发出去：新任务、`-c` 打开的、`--resume ID` 打开的都是这样。`--resume`
不带 id 时，它先留在输入框里，等你挑好任务再发给那个任务。在还没信任
的目录里，它会先问你是否信任；选 "No, exit" 就直接退出。

| 参数 | 含义 |
|---|---|
| `-c`、`--continue` | 打开本目录里最近开始的那场对话 |
| `-r`、`--resume [ID]` | 打开任务 `ID`；不给 id 就从列表里挑 |
| `--server URL` | 操作一台 `noeta serve` 服务端上的任务，而不是在本机跑（token 取自 `NOETA_SERVER_TOKEN`，没有就读同一台机器上服务端的 `~/.noeta/server-token`）。新任务用服务端的模型和目录，除非 `--model` / `--cwd` 另有指定。会影响本机所跑任务的参数（`--plugin-dir`，以及权限、设置、MCP、`--agents`、系统提示词文件这些参数）不能和它一起用 |
| `--agent NAME` | 新任务用的代理（默认取设置文件里的 `agent`，没有就是 `main`） |
| `--plugin-dir PATH` | 只在这次运行里加载某个插件文件、插件目录或一组插件目录（可重复） |
| `-v`、`--version` | 打印版本号后退出 |

下一节里的权限、目录、设置、MCP、`--agents` 和系统提示词文件参数，在界面模式下和 `-p` 一样生效。
只有无界面模式才用得上的参数（`--max-turns`、`--json-schema`、`--permission-prompt-tool` 等）
也接受，但不起作用，界面的提示里会列出来。

## 无界面模式：`noeta -p` {#headless-noeta-p}

Claude Code 的打印模式和 stream-json 协议（`noeta/host/stream_json.py`）。带 `-p`、
`--input-format stream-json` 或任意 `--output-format` 时进入无界面模式。

| 参数 | 含义 |
|---|---|
| `-p`、`--print` | 不开界面，跑提示词并打印结果 |
| `prompt` | 提示词；带 `-p` 时，没给就从标准输入读，给了的话标准输入里管道送进来的内容另起一行接在后面 |
| `--output-format {text,json,stream-json}` | `text`（默认）：只打印回答；`json`：一个结果对象；`stream-json`：每行一条 JSON 消息（需要 `--verbose`） |
| `--input-format {text,stream-json}` | `stream-json`：从标准输入按行读用户消息和控制请求（需要 `--output-format stream-json`） |
| `--include-partial-messages` | 模型生成过程中也输出 `stream_event` 消息 |
| `--replay-user-messages` | 把从标准输入读到的用户消息回显出来 |
| `--verbose` | `--output-format stream-json` 必须带，和 Claude Code 一样 |
| `--max-turns N` | 一轮里最多调 N 次模型；还要再调就让这一轮失败（结果的 subtype 是 `error_max_turns`） |
| `--max-budget-usd X` | 整个任务树花到 X 美元就停（结果的 subtype 是 `error_max_budget_usd`） |
| `--json-schema SCHEMA` | 回答必须符合的 JSON Schema（根节点 `"type": "object"`）；模型通过 `StructuredOutput` 工具作答，结果里带 `structured_output` |
| `--allowedTools`、`--allowed-tools RULES…` | 放行规则，如 `"Bash(git *) Edit"`；规则之间用空格或逗号分隔，括号里的空格保留 |
| `--disallowedTools`、`--disallowed-tools RULES…` | 禁止规则；只写工具名时连工具一起去掉 |
| `--tools NAMES…` | 提供哪些工具：`default` 全部，`""` 一个都不给，否则只给列出的 |
| `--add-dir DIR…` | 工具还可以读写的其他目录 |
| `--permission-mode MODE` | 同上 |
| `--dangerously-skip-permissions` | 以 `bypassPermissions` 模式开始 |
| `--permission-prompt-tool TOOL` | `stdio`：由客户端回答权限确认（`can_use_tool` 控制请求）；MCP 工具名 `mcp__<server>__<tool>`：由那个工具回答。不给的话，凡是要问人的都直接拒绝，这一轮继续 |
| `--permission-prompts {host,none}` | `none` 时忽略 `--permission-prompt-tool`（默认 `host`） |
| `-c`、`--continue` | 接着本目录里用同一个代理最近开始的任务 |
| `-r`、`--resume ID` | 接着任务 `ID` |
| `--fork-session` | 配合 `--resume` / `--continue`：从那场对话分出一个新任务继续 |
| `--session-id ID` | 新任务的 id（沿用 Claude Code 的参数名；在 noeta 里它是任务 id） |
| `--agent NAME` | 要跑的代理（默认取设置文件里的 `agent`，没有就是 `main`） |
| `--system-prompt-file PATH` | 用文件内容替换 main 代理的系统提示词 |
| `--append-system-prompt-file PATH` | 把文件内容追加到系统提示词后面 |
| `--settings FILE_OR_JSON` | 再加一层设置，位于项目设置之上、管理员设置之下 |
| `--setting-sources user,project,local` | 读哪些设置文件；管理员的总会读。去掉 `user` 时也不读 `~/.noeta/agents`；去掉 `project` 时不读项目的代理和 `.noeta/mcp.json` |
| `--mcp-config FILE_OR_JSON…` | MCP 服务器：含 `mcpServers` 的 JSON 文件或字符串；同名时后面的为准 |
| `--strict-mcp-config` | 只用 `--mcp-config` 里的服务器，不读设置文件和项目里的 |
| `--agents JSON` | 用 JSON 对象定义子代理：名字 → `{description, prompt, …}`（代理文件的字段）；最后加载，同名时以它为准 |
| `--plugin-dir PATH` | 同上 |

`-p` 不能和 `--server` 一起用：`-p` 总是在本进程里跑任务。

**接受但忽略的参数**，是为了让 Claude Code 的命令行原样能跑；用到时会在 stderr 上点名：
`--fallback-model`、`--betas`、`--max-thinking-tokens`、`--thinking`、`--thinking-display`、
`--plugin-url`、`--task-budget`、`-d` / `--debug`（可带一个过滤值）、`--debug-file`、`--name`、`--autocompact`、`--file`、
`--system-prompt-snapshot`（这些带值），以及 `--include-hook-events`、`--no-session-persistence`、
`--session-mirror`、`--bare`、`--allow-dangerously-skip-permissions`、
`--exclude-dynamic-system-prompt-sections`、`--forward-subagent-text`、`--disable-slash-commands`。

**退出码：** 结果成功为 0；结果出错、参数的值没法用、没配模型、提示词为空、没有可接着的任务时为 1；
参数不认识或命令行写错（argparse 报错）时为 2。用 `--input-format stream-json` 时，标准输入结束后
按最后一个结果定退出码（一个提示词都没跑就是 0）。

**结果对象**（`json` 格式，以及 `stream-json` 里每个提示词的最后一行）字段和 Claude Code 的一样：
`type: "result"`、`subtype`（`success`、`error_during_execution`、`error_max_budget_usd` 等）、
`is_error`、`result`、`duration_ms`、`duration_api_ms`、`num_turns`、`total_cost_usd`、`usage`、
`modelUsage`、`permission_denials`、`stop_reason`、`session_id`（根任务的 id），有 schema 时还有
`structured_output`。

**stream-json 客户端能发的控制请求：** `initialize`、`interrupt`、`set_permission_mode`、
`set_model`、`set_max_thinking_tokens`、`rewind_files`、`get_context_usage`、`get_session_cost`、
`list_models`、`mcp_status`。

## `noeta acp`

在标准输入输出上提供 Agent Client Protocol，给 Zed 等支持 ACP 的编辑器用（需要 `acp` 组件）。
本机模式下它跑一个 `LocalApp`，并加上编辑器提供的 MCP 服务器；编辑器里的每次会话对应一个任务。

| 参数 | 含义 |
|---|---|
| `--server URL` | 改为操作 `noeta serve` 服务端的任务（token 取自 `NOETA_SERVER_TOKEN`，没有就读同一台机器上服务端的 `~/.noeta/server-token`）；用服务端的模型，除非 `--model` 另行指定；不发送编辑器的本地目录 |
| `--cwd PATH` | 同通用参数（写在 `acp` 后面也行，后写的为准） |
| `--model MODEL` | 同通用参数（写在 `acp` 后面也行） |

编辑器怎么配置见[网页、桌面与编辑器](../guides/front-ends.md)。

## `noeta serve`

通过 HTTP 和 WebSocket 提供客户端接口（需要 `server` 组件）；同一个进程里还会跑 `config.toml`
启用的渠道，没加 `--no-worker` 时也跑 worker。接口清单见 [HTTP / WebSocket 接口](./http-api.md)。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--host HOST` | `127.0.0.1` | 绑定的地址 |
| `--port PORT` | `8642` | 端口 |
| `--no-worker` | 关 | 只提供接口，worker 在别处跑（`noeta worker`） |
| `--no-token` | 关 | 不要 bearer token；只有 `--host` 是回环地址时才允许 |
| `--allow-host NAME` | 无 | 除服务端自己的名字之外，还认可哪个 `Host` 头名字（可重复）。自己的名字：绑回环地址时是回环名字，绑其他地址时还包括本机的主机名、完整域名和本机地址。其余 `Host` 一律拒绝 |
| `--allow-origin ORIGIN` | 无 | 允许调用接口的网页来源（`https://host:port`，可重复） |
| `--disable-bypass-permissions` | 关 | 不管请求怎么要，都拒绝 `bypassPermissions`。旧的 `--allow-bypass-permissions` 仍然接受，不起作用 |
| `--allow-plugin-install` | 关（`--host` 是回环地址、且没带 `--allow-host` / `--allow-origin` 时开） | 允许请求安装和删除插件（`POST /plugins/install`、`DELETE /plugins/{name}`） |
| `--web DIR` | 包里自带的网页（有的话） | 在其余路径上提供这个目录里的文件（构建好的网页，`web/dist`）。不给的话，就用安装包里自带的那份：从 PyPI 装的都有，源码目录里要先跑一次 `make web-bundle` |

token 取自 `NOETA_SERVER_TOKEN`；没有的话用 `~/.noeta/server-token`（只有自己能读）里的那个，第一次启动时生成；
删掉这个文件再重启就会换一个新的。每次启动都会在 stderr 上打印能打开的地址，令牌放在 `#` 后面（绑非回环地址时，
本机每个名字和地址各一行；用 `--no-token` 时不带令牌）：

```text
$ noeta serve
noeta: web app at http://127.0.0.1:8642/#token=…
```

网页读走令牌后会把它从地址栏里去掉，不会出现在服务端日志里。请求新建任务时没给 `cwd` 的话，任务在自己的目录里跑：
`~/.noeta/scratch/<YYYY-MM-DD>/<任务 id 前 8 位>/`。加了 `--disable-bypass-permissions` 时，不管通过什么途径要
`bypassPermissions`，权限插件自己都会拒绝。

## `noeta service`

把 `noeta serve` 装成当前用户的系统服务：登录时启动，崩了自动重启。Linux 上是 systemd 用户服务
（`~/.config/systemd/user/noeta.service`），macOS 上是 launchd 的 LaunchAgent
（`~/Library/LaunchAgents/dev.noeta.serve.plist`，输出写到 `~/.noeta/logs/serve.log`）。

| 命令 | 作用 |
|---|---|
| `noeta service install [serve 的参数]` | 写好服务（用这些参数跑 `noeta serve`；通用参数写在 `service` 前面）并启动；再跑一次就替换掉原来的。Linux 上没开 lingering 时，会提醒退出登录后服务会停，并给出 `loginctl enable-linger` 命令 |
| `noeta service status` | 服务的状态，以及能打开的地址（按服务的参数和令牌算出来） |
| `noeta service logs` | 最后 200 行输出，然后持续跟着看（`--no-follow`：看完这些就退出） |
| `noeta service start` / `stop` / `restart` | 启动、停止、重启 |
| `noeta service uninstall` | 停掉并删除服务 |

服务在你的家目录里运行，环境变量是执行 `install` 那个 shell 里的 `NOETA_*`、`ANTHROPIC_*`、`OPENAI_*`、
`DEEPSEEK_*`、代理（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，大小写都算）、`PATH` 和 `HOME`，
存在只有自己能读的文件里（Linux 是服务文件旁边的 `noeta.env`，macOS 就是那个 plist）：改了其中哪个，就再跑一次
`install`。既没有 systemd 用户实例也没有 launchd 时，它会直接说明，并建议在 `tmux` 里跑 `noeta serve`。
用法讲解见[作为服务端运行](../guides/server.md#让它一直跑着-noeta-service)。

## `noeta update`

把 noeta 升到 PyPI 上的最新版，装的时候带了什么就还带什么：

| 当初怎么装的 | 它会执行 |
|---|---|
| `uv tool install` | `uv tool install --reinstall 'noeta[<extra>]'`，`uv-receipt.toml` 里记的每个 `--with`、Python 版本和包索引都原样带上，只去掉版本锁定 |
| pip | `pip install -U 'noeta[<已装的 extra>]'`（环境里没有 pip 就用 `uv pip install`） |
| 源码目录或可编辑安装 | 什么都不动，只打印该执行的命令 |

PyPI 上没有更新的版本时，它会说 `noeta is up to date`；连不上 PyPI 时什么也不改（代理照
`HTTPS_PROXY` / `ALL_PROXY` 走）。升级完会重启已装好的 `noeta service`。

## `noeta worker`

从存储里认领并运行任务，直到收到 SIGINT 或 SIGTERM；同时运行 `config.toml` 里启用的渠道。配合
Postgres 存储就能把任务分到多台机器上跑。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--queue NAME` | `default` | 从哪个队列认领（可重复） |
| `--concurrency N` | `8` | 同时跑几个任务 |
| `--lease SECONDS` | `30` | 一次认领不续约能维持多久；worker 挂了，它的任务大约等这么久后被别人接手 |
| `--snapshot-every EVENTS` | `100` | 最多每隔多少个事件写一次任务的重放快照；`0` 表示不写 |
| `--restart-limit N` | `3` | 工人跑到一半死掉后，任务最多连续重启这么多次（期间一步也没做完）；再崩一次就以 `failed`（`restart_limit`）结束；`0` 表示不限（[原因](../how-it-works/recovery.md#a-task-that-keeps-killing-its-worker)） |
| `--disable-bypass-permissions` | 关 | 不许这里跑的任务用 `bypassPermissions`（旧的 `--allow-bypass-permissions` 仍然接受，不起作用） |

worker 也会每天清理一次本机存下来的旧工具输出（见[清理](../guides/server.md#清理)）。退出时，正在跑的任务立刻交还；为后台作业保留着认领的任务会被释放，并在下一轮被告知作业已经没了。

## `noeta trust`

把当前目录（或 `--cwd`）加进 `~/.noeta/trusted.json`。之后它的 `.noeta/settings*.json`、
`.noeta/mcp.json`（还要你同意）、代理、技能、命令、提示词覆盖和插件才会加载（[配置](../start/configuration.md#trust)）。

## `noeta plugins`

列出每个插件及其来源（某个包，或某个插件文件）、所有渠道（标注 `(channel)`），以及找到的 Claude
Code 插件目录（带版本号）。加载失败的插件文件打印到 stderr，写明原因；缺模块时还会给出补装命令。

## `noeta plugin`

把插件装进 `~/.noeta/plugins/`，固定到某个版本，安装过程不执行插件里的任何东西。

| 命令 | 含义 |
|---|---|
| `noeta plugin add SOURCE` | 从 `<git-url>@<commit>`（https 或 ssh，完整的提交号：40 位十六进制，SHA-256 仓库是 64 位）安装，或复制一个本地路径 |
| `noeta plugin add NAME` | 从配好的插件市场里装名叫 `NAME` 的插件（好几个市场都有时，用 `--marketplace` 指定一个） |
| `noeta plugin add --marketplace MARKETPLACE NAME` | 从配好的某个市场（写它的名字），或某个 Claude Code `marketplace.json`（本地路径、`<git-url>@<commit>`，或按默认分支读的 git 地址）安装 `NAME`；只读取 marketplace，不执行 |
| `  --commit SHA` | marketplace 条目没固定提交时，用这个提交（条目里的 `ref`，也就是分支或标签，不会去跟）；和条目固定的提交不一致就拒绝 |
| `  --config KEY=VALUE` | Claude Code 插件的 `userConfig` 值（可重复）；没给的在终端上逐个问 |
| `noeta plugin configure NAME [--config KEY=VALUE]` | 重新设置 Claude Code 插件的 `userConfig` |
| `noeta plugin list` | 用 `noeta plugin add` 装的插件 |
| `noeta plugin remove NAME` | 删掉一个 |
| `noeta plugin marketplace add SOURCE` | 加一个插件市场：本地路径、`<git-url>@<commit>`，或按默认分支读的 git 地址；名字取它 `marketplace.json` 里的 `name`，和已有的重名就替换掉 |
| `noeta plugin marketplace list` | 列出每个市场和它里面的插件（版本、固定的提交、装没装）；读不出来的市场在 stderr 上说明 |
| `noeta plugin marketplace remove NAME` | 删掉一个市场（从它装的插件留着） |

插件市场的列表存在 `~/.noeta/marketplaces.json`；这个文件还不存在时，只有官方市场 `noeta-plugins`（`https://github.com/initxy/noeta-plugins`）。`userConfig` 的值存在 `~/.noeta/settings.json` 的 `pluginConfigs` 里，敏感的存进系统钥匙串。详见
[写一个插件](../guides/plugins.md)。

## `noeta mcp`

和 `claude mcp` 一样管理 MCP 服务器，只是写在 noeta 自己的位置。

| 命令 | 含义 |
|---|---|
| `noeta mcp add NAME COMMAND_OR_URL [ARGS…]` | 添加一个服务器 |
| `  -s`、`--scope {local,project,user}` | `local`（默认）：`.noeta/settings.local.json`；`project`：`.noeta/mcp.json`，随仓库共享；`user`：`~/.noeta/settings.json` |
| `  -t`、`--transport {stdio,sse,http,ws}` | 默认 `stdio` |
| `  -e`、`--env KEY=value…` | stdio 服务器的环境变量 |
| `  -H`、`--header "Name: value"…` | 远程服务器的请求头 |
| `  --client-id ID` | HTTP / SSE 服务器的 OAuth 客户端 id |
| `  --client-secret` | 提示输入 OAuth 客户端密钥（或者设 `NOETA_MCP_CLIENT_SECRET`） |
| `  --callback-port N` | OAuth 回调用的固定端口 |
| `noeta mcp add-json [-s SCOPE] [--client-secret] NAME JSON` | 用一段 JSON 配置添加服务器 |
| `noeta mcp list` | 列出已配置的服务器并逐个检查；项目没被信任时，它的 `local` 和 `project` 服务器不启动，显示为 ``project not trusted (not started; run `noeta trust` here)`` |
| `noeta mcp get NAME` | 查看一个（几个文件都配了同名服务器时，显示运行时会启动的那个：managed > `--settings` > local > project > user） |
| `noeta mcp remove [-s SCOPE] NAME` | 删掉一个 |
| `noeta mcp login [--no-browser] NAME` | 登录（OAuth）；`--no-browser` 只打印地址，不打开浏览器 |
| `noeta mcp logout NAME` | 清掉登录信息 |

客户端密钥和登录 token 存在 `~/.noeta/mcp-auth/` 下，从不写进设置文件。详见 [MCP 服务器](../guides/mcp.md)。

## 出错时 {#errors}

noeta 没料到的错误只打印一行，再告诉你完整报告存在哪：

```text
noeta: unexpected error: RuntimeError: …
noeta: the full report is in ~/.noeta/logs/crash-20261109-101500-4242.txt (--verbose or NOETA_DEBUG=1 prints it here)
```

报告里有调用栈、版本号和完整命令行。命令行里任意位置加 `--verbose`，或者设 `NOETA_DEBUG=1`，调用栈也会
直接打在终端上。按 Ctrl+C 退出时退出码是 130。

## 斜杠命令 {#slash-commands}

终端界面自带的命令（`tui/src/noeta_tui/slash.py`）：

| 命令 | 作用 |
|---|---|
| `/help` | 显示帮助和命令列表 |
| `/status` | 版本、模型、任务、目录和设置 |
| `/model [MODEL]` | 选择或设置模型，下一次请求起生效；不带参数时列出所有 provider 的模型 |
| `/effort [LEVEL]` | 选择或设置 effort（`low` … `max`） |
| `/context` | 下一次请求里都装了什么，按格子显示 |
| `/cost` | 本任务及其子代理的 token 和花费 |
| `/memory` | 列出项目说明和记忆文件；在 `$EDITOR` 里打开其中一个 |
| `/config [edit user\|project\|local]` | 查看设置；编辑某个设置文件 |
| `/permissions` | 当前权限模式和规则 |
| `/agents` | 这个前端跑着哪些代理 |
| `/mcp` | MCP 服务器状态：已连接、失败、等待登录 |
| `/hooks` | 已配置的 hooks（只读） |
| `/rewind` | 回到某次提问之前：恢复对话、代码或两者（也可以连按两次 Esc） |
| `/clear`、`/new` | 开一个新任务 |
| `/resume [N]` | 挑一个任务接着聊；给了 `N`（列表里的序号）或任务 id 就直接打开 |
| `/stop` | 停下这一轮（也可以按 Esc） |
| `/reload-plugins` | 重新加载插件、代理和设置文件，下一轮起生效；`env`、`model`、`agent`、`sandbox` 变了要重启才生效（界面会提示） |
| `/exit`、`/quit` | 退出 |

代理的插件提供的命令（由 `noeta/core/commands.py` 列出和展开；每个前端都会显示，
`POST /tasks/{id}/messages` 也能执行）：

| 命令 | 插件 | 作用 |
|---|---|---|
| `/compact [重点]` | `compaction` | 现在就压缩对话，可以指定摘要的侧重点 |
| `/plan [需求]` | `plan_mode` | 切到计划模式；带上需求就直接开始规划 |
| `/init` | `markdown_commands` | 为项目写一份 `NOETA.md`，已有就改进它 |
| `/review [PR \| 分支 \| 路径]` | `markdown_commands` | 审查改动里的 bug |
| `/loop [间隔] [提示词]` | `schedule` | 按固定间隔反复执行一个提示词或命令 |
| `/hooks` | `hooks` | 已配置的 hooks |
| `/mcp` | `mcp` | MCP 服务器状态 |
| `/mcp__<server>__<prompt>` | `mcp` | 某个 MCP 服务器提供的 prompt |
| `/<skill>`、`/<plugin>:<skill>` | `skills` | 按名字运行某个技能 |
| `/<command>` | `markdown_commands` | `~/.noeta/commands`、`.noeta/commands` 或 Claude Code 插件里的 `commands/*.md` |

终端界面的按键：Esc 停下这一轮；连按两次 Esc 清空输入框，输入框已空时打开 `/rewind`；Shift+Tab
切换权限模式；Ctrl+C 清空输入框（输入框为空时连按两次退出）；输入以 `!` 开头时直接执行 shell 命令；
Ctrl+R 搜索以前输入过的提示词；Ctrl+O 展开或收起工具调用；Ctrl+T 显示或隐藏待办列表；输入框为空时按 ↓
（或 Ctrl+B）进到子代理列表，回车看那个代理做了什么；Ctrl+V 粘贴图片。
