# 配置

noeta 的设置从哪里来、冲突时谁说了算、项目里的哪些配置要等你信任之后才生效。每个配置项和环境变量
的完整清单见[配置项](../reference/config.md)。

## 两类设置

noeta 有两套配置，管的是不同的事：

- **noeta 自己的：用哪个模型、哪个 provider、哪个存储、哪些插件。** 来自命令行参数、`NOETA_*`
  环境变量和 `~/.noeta/config.toml`（`noeta/cli/config.py`）。
- **Claude Code 格式的：agent 能做什么、怎么做。** 权限规则、hooks、MCP 服务器、沙箱、`env`、
  回答语言、提交署名行、默认代理。它们写在 Claude Code `settings.json` 格式的设置文件里，只是从
  noeta 自己的目录读（`noeta/cli/settings_files.py`）。代理文件、技能、命令、项目说明文件和插件
  目录也都是 Claude Code 的格式。

<Diagram name="start-config-places" caption="越往上优先级越高。项目的两个设置文件要在 noeta trust 之后才读。" />

## 模型、provider、存储：命令行参数 > 环境变量 > `config.toml`

模型、provider、地址、API key、存储和 effort，按下面的顺序取第一个有值的
（`noeta/cli/config.py` 里的 `load_settings`）：

1. 命令行参数：`--model`、`--provider`、`--base-url`、`--store`、`--effort`；
2. 环境变量：`NOETA_MODEL`、`NOETA_PROVIDER`、`NOETA_BASE_URL`、`NOETA_API_KEY`、`NOETA_STORE`、
   `NOETA_EFFORT`、`NOETA_MODELS_FILE`；
3. `~/.noeta/config.toml` 里的键：`model`、`provider`、`base_url`、`api_key`、`store`、`effort`、
   `models_file`；
4. 只对模型：上面都没指定时，用设置文件里的 `model`。

没配 API key 时，`anthropic` 会去读 `ANTHROPIC_API_KEY`，`openai_compat` 会去读 `OPENAI_API_KEY`。
还是没有就启动失败，并告诉你该在哪儿设。存储默认是 `sqlite:///~/.noeta/tasks.db`。

一个完整的 `config.toml` 大概长这样：

```toml
# ~/.noeta/config.toml
model = "claude-sonnet-5-5"
provider = "anthropic"            # 或 openai_compat、openai_responses，或某个表的名字
effort = "high"
# store = "postgres://noeta:…@db.internal/noeta"

[providers.deepseek]              # 更多 provider；每个模型发给提供它的那家
family = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key = "{env:DEEPSEEK_API_KEY}"
models = ["deepseek-chat", { id = "deepseek-reasoner", context_window = 128000, max_output_tokens = 64000 }]

[plugins.shell]                   # 按插件名写它的参数
background = true
[plugins.web]
enabled = false                   # 对所有代理关掉

[agents.main]
add_plugins = ["my_plugin"]       # 给某个代理加插件，不用复制它的文件
```

一个模型 id 怎么找到对应的 provider，任务又怎么记下这个选择、让每台 worker 都发到同一处，见
[模型与 provider](../guides/models.md) 和 [provider 与模型路由](../how-it-works/models.md)。插件表的
写法见[插件设置](../reference/config.md#plugin-settings)。

`config.toml` 只有 `~/.noeta/` 里那一个。项目里没有 `config.toml`；项目能设的东西写在它的设置文件里。

## 设置文件（Claude Code 的格式）

设置文件是 Claude Code `settings.json` 格式的 JSON 对象。noeta 按下面的顺序读，后读的覆盖先读的：

| # | 文件 | 归谁 | 什么时候读 |
|---|---|---|---|
| 1 | `~/.noeta/settings.json` | 你自己 | 总是 |
| 2 | `<项目>/.noeta/settings.json` | 项目的，提交进仓库 | 项目被信任之后 |
| 3 | `<项目>/.noeta/settings.local.json` | 项目的，不进 git | 项目被信任之后 |
| 4 | `--settings` 给的文件或 JSON 字符串 | 你自己，只对这一次 `noeta -p` | 给了就读 |
| 5 | 机器管理目录里的 `managed-settings.json` | 这台机器的管理员 | 总是 |

机器管理目录在 Linux 上是 `/etc/noeta`，macOS 上是 `/Library/Application Support/Noeta`，Windows
上是 `C:\Program Files\Noeta`。`noeta -p` 的 `--setting-sources user,project,local` 可以挑只读 1–3
里的哪几个；管理员的文件总会读。

几个文件怎么合起来：

- 普通的键后者覆盖前者：`model`、`agent`（前端默认用的代理）、`outputStyle`、`language`、
  `attribution` 的每一行、`env` 里的每个变量、`permissions.defaultMode`。
- 权限规则列表（`permissions.allow`、`deny`、`ask`）和 `permissions.additionalDirectories` 是
  累加的，去重。
- 任何一个文件写了 `permissions.disableBypassPermissionsMode: "disable"`，bypass 模式就关掉了，
  没有哪个文件能再打开。
- `sandbox` 逐个列表合并，但项目里的文件只能收紧：不能关掉沙箱，不能放开更多路径、域名或命令，
  也不能设 `bwrapPath`（只有管理员文件能设）。
- hooks 和 MCP 的同意设置（`enabledMcpjsonServers`、`disabledMcpjsonServers`、
  `enableAllProjectMcpServers`）由 hooks 插件和 MCP 插件从同一组文件里读。

**`env` 最先生效。** 合并后的 `env` 会在读其他任何东西之前设进 noeta 的进程，和 Claude Code 一样：
设置文件里写 `"env": {"NOETA_MODEL": "…"}` 就等于设了这个环境变量，noeta 启动的每个命令、hook、
MCP 服务器也都能看到。

每个设置项具体做什么在使用指南里：权限和 hooks 见[权限与 hooks](../guides/permissions.md)，MCP 见
[MCP 服务器](../guides/mcp.md)，沙箱见[命令在哪里执行](../guides/sandbox.md)。noeta 读哪些键，列在
[配置项](../reference/config.md#settings-files)。

## 信任 {#trust}

克隆下来的仓库不能自己改变你的 agent 能做什么，也不能自己启动进程。所以项目自带的配置要等你信任它
之后才加载：

```bash
cd ~/code/some-project
noeta trust
```

或者在终端界面第一次打开这个目录、问你时选"Yes, I trust this folder"。信任过的目录记在
`~/.noeta/trusted.json`（`noeta/cli/trust.py`），按 noeta 启动时所在的那个目录精确匹配。

| 项目里的东西 | 信任之前 | 信任之后 |
|---|---|---|
| `.noeta/settings.json`、`.noeta/settings.local.json` | 不读 | 读 |
| `.noeta/plugins/`（Python 插件文件、Claude Code 插件目录） | 不加载，并提示 | 加载 |
| `.noeta/agents/` | 不加载，并提示 | 加载 |
| `.noeta/skills/`、`.agents/skills/` | 不加载，并提示 | 加载 |
| `.noeta/prompts/`（提示词覆盖） | 不读 | 读 |
| `.noeta/mcp.json` | 服务器永远不启动 | 还要项目自己的设置文件点头才启动：`enableAllProjectMcpServers: true`，或者把它的名字写进 `enabledMcpjsonServers` |
| `NOETA.md`、`.noeta/NOETA.md`、`NOETA.local.md`、`.noeta/rules/`、`AGENTS.md` | 读 | 读 |

项目说明文件不管信不信任都读，就像 Claude Code 读 `CLAUDE.md`：它们是给模型看的文字，不是设置。
在你自己的 `~/.noeta/settings.json` 里对某个项目 MCP 服务器点的头不算数，这样一个仓库就没法借用同名
服务器、蹭你的同意启动它。

设置文件和插件目录只在前端或服务端启动时、按启动目录读一次。提示词覆盖、技能和权限检查看的则是每个
任务自己的工作目录，所以在服务端上，跑在未信任目录里的任务拿不到那个目录的提示词和技能。

## 项目说明：`NOETA.md` 和 `AGENTS.md`

项目说明和 Claude Code 的 `CLAUDE.md` 是一回事，只是换成了 noeta 的文件名
（`noeta/plugins/instructions/files.py`）：

| 文件 | 类别 |
|---|---|
| 机器管理目录里的 `NOETA.md` 和 `.noeta/rules/` | 管理员的 |
| `~/.noeta/NOETA.md` 和 `~/.noeta/rules/` | 你自己的，对所有项目生效 |
| 从最外层上级目录一路到工作目录，每一层的 `NOETA.md` 或 `.noeta/NOETA.md` | 项目的 |
| `AGENTS.md`，只在上面两种都没有的目录里读 | 跨工具通用的那份 |
| `NOETA.local.md` | 你自己对这个项目的，不进 git |
| `.noeta/rules/*.md` | 规则，可以在 frontmatter 里用 `paths:` 限定只对某些文件生效 |

文件里的 `@path` 会把另一个文件一起读进来（最多嵌套五层）。从项目文件出发的引用必须落在工作目录或
发起这串引用的文件所在目录之内。`/init` 会为项目写一份 `NOETA.md`。详见
[项目说明与记忆](../guides/memory-and-instructions.md)。

## noeta 还会看哪些地方

`~/.noeta` 下的一切都属于 noeta；设 `NOETA_HOME` 可以把它挪到别处。

| 路径 | 放的是什么 |
|---|---|
| `~/.noeta/config.toml` | 模型、provider、存储、插件表 |
| `~/.noeta/settings.json` | 你的设置（Claude Code 格式）；Claude Code 插件的 `pluginConfigs` 也在这 |
| `~/.noeta/tasks.db` | 默认的 sqlite 存储：所有任务的日志 |
| `~/.noeta/trusted.json` | 信任过的项目 |
| `~/.noeta/agents/`、`<项目>/.noeta/agents/` | 代理文件；同名时后读的为准 |
| `~/.noeta/skills/`、`~/.agents/skills/`、`.noeta/skills/`、`.agents/skills/` | 技能，项目及其上级目录里的都算 |
| `~/.noeta/commands/`、`<项目>/.noeta/commands/` | 斜杠命令（Claude Code 的 `commands/*.md`） |
| `~/.noeta/prompts/`、`<项目>/.noeta/prompts/` | 按提示词名字覆盖提示词 |
| `~/.noeta/plugins/`、`<项目>/.noeta/plugins/` | 插件文件和 Claude Code 插件目录 |
| `~/.noeta/plugin-data/<name>/` | Claude Code 插件的 `${CLAUDE_PLUGIN_DATA}` |
| `~/.noeta/mcp-auth/` | MCP 登录凭据和 OAuth 客户端密钥（只有你能读） |
| `~/.noeta/server-token` | `noeta serve` 的 token 的 SHA-256 |
| `~/.noeta/cache/models/` | 各 provider 的模型列表，缓存一天 |
| `~/.noeta/tool-results/<任务>/` | 太长、没法直接返回的工具输出 |
| `~/.noeta/plans/`、`~/.noeta/projects/<项目>/memory/`、`~/.noeta/agent-memory/` | 计划文件和自动记忆 |
| `~/.noeta/device_id` | 一个随机的设备 id，Claude Code 也会在 `metadata.user_id` 里发这个 |

多台机器要用同样的插件和代理，就得看到同一份 `~/.noeta`（复制过去，或者把 `NOETA_HOME` 放到共享
存储上）；哪些部分必须共享，见[作为服务端运行](../guides/server.md)。

noeta 从不读 `~/.claude/`、`.claude/`、`CLAUDE.md`、`.mcp.json`，也不读 `CLAUDE_*` 环境变量
（[ADR-004](https://github.com/initxy/noeta/blob/main/docs/adr/004-noeta-reads-only-its-own-config-places.md)）。
想复用 Claude Code 的文件，就复制或链接到 noeta 的目录里，格式是一样的。

## 密钥：`{env:…}` 和 `{keyring:…}`

插件参数、渠道参数、provider 表，以及代理文件里的插件设置，都可以用引用代替密钥本身：

```toml
[providers.claude]
family = "anthropic"
api_key = "{env:ANTHROPIC_API_KEY}"

[channels.chat_bridge]
token = "{keyring:chat-bridge/bot}"     # 需要 keyring 组件
```

- `{env:NAME}` 在创建插件时换成那个环境变量的值；变量没设就报错，并指出是哪里用到的。
- `{keyring:service/user}` 从操作系统的钥匙串里读；需要装 `keyring` 组件。
- 配置文件和任务日志里存的只是引用，不是密钥。
- 被引用的变量会被当成 noeta 自己的凭据：noeta 启动的命令、hook、MCP 服务器都继承不到它。
  `NOETA_API_KEY`、`NOETA_STORE`、`NOETA_SERVER_TOKEN`、`NOETA_MCP_CLIENT_SECRET`、
  `NOETA_WEB_SEARCH_API_KEY` 也始终这样屏蔽（`noeta/toolkit/local_env.py`）。

`config.toml` 顶层的 `api_key` 只能写明文；不想把 key 写进文件，就用 `[providers.<name>]` 表、
`NOETA_API_KEY`，或者 provider 自己的环境变量。

## 机器管理员说了算：`[policy]`

管理员可以在机器管理目录的 `config.toml` 里按名字放行或禁止插件，优先于其他所有设置
（`noeta/cli/plugin_policy.py`）：

```toml
# /etc/noeta/config.toml
[policy]
allowed_plugins = ["my_company_plugin"]   # 设了之后，第三方插件只加载这些
blocked_plugins = ["web"]                 # 一律不加载，内置的也一样
```

`allowed_plugins` 不会挡住 noeta 自带的插件；`blocked_plugins` 什么都能挡。插件文件在导入之前按
文件名检查，插件、渠道和 Claude Code 插件按名字检查。
