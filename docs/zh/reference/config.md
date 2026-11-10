# 配置项

`~/.noeta/config.toml` 的每个键、provider 表的每个键、noeta 读取的设置文件键、插件设置怎么叠加，
以及 noeta 读取的每个环境变量。它们各自从哪来、谁优先，见[配置](../start/configuration.md)。

## `config.toml`

`~/.noeta/config.toml`（即 `$NOETA_HOME/config.toml`）每个 `noeta` 命令都会读
（`noeta/cli/config.py`）。命令行参数和环境变量优先于顶层的键。

### 顶层键

| 键 | 类型 | 默认 | 含义 | 被谁覆盖 |
|---|---|---|---|---|
| `model` | 字符串 | 无（总得有个地方设） | 新任务用的模型，id 或别名 | `--model`、`NOETA_MODEL` |
| `provider` | 字符串 | `anthropic` | 默认 provider：一种类型（`anthropic`、`openai_compat`、`openai_responses` 或第三方包提供的），或某个 `[providers.<name>]` 表的名字 | `--provider`、`NOETA_PROVIDER` |
| `base_url` | 字符串 | 该类型的默认地址 | 默认 provider 的地址：`anthropic` 只写主机，`openai_compat` 写到 `…/v1`，`openai_responses` 写完整的 `…/v1/responses` | `--base-url`、`NOETA_BASE_URL` |
| `api_key` | 字符串 | 无 | 默认 provider 的 key：明文、`{env:NAME}` 或 `{keyring:service/user}`；不写时 `anthropic` 读 `ANTHROPIC_API_KEY`，`openai_compat` 读 `OPENAI_API_KEY`；`openai_responses` 不读任何变量，必须写 | `NOETA_API_KEY` |
| `extra_headers` | 字符串表 | 无 | 默认 provider 每次请求都带的请求头；值可以写成 `{env:…}` / `{keyring:…}` 引用 | — |
| `store` | 字符串 | `sqlite:///~/.noeta/tasks.db` | `sqlite:///path` 或 `postgres://…` | `--store`、`NOETA_STORE` |
| `effort` | 字符串 | 模型自己的 | `low`、`medium`、`high`、`xhigh` 或 `max`，对终端界面、`-p`、`acp`、`serve`、`worker` 新建的任务生效（见 [noeta 命令](./cli.md#通用参数)） | `--effort`、`NOETA_EFFORT` |
| `models_file` | 路径 | 无 | 描述更多模型的 JSON 文件（见下） | `NOETA_MODELS_FILE` |
| `list_models` | 布尔 | `true` | 对目录里没有的模型，去读默认 provider 的 `/models` 列表 | — |
| `prompt_cache_ttl` | `"5m"` 或 `"1h"` | 接口默认的（`5m`） | 仅 `anthropic`：一小时的提示词缓存，每次写入计费更高 | — |
| `prompts` | 表 | 无 | 对这个前端启动的所有任务生效的提示词覆盖，按提示词名写（`"system/main.append" = "…"`） | `--system-prompt`、`--append-system-prompt` |

另外还有这些表：`[providers.<name>]`、`[plugins.<name>]`、`[agents.<name>]`、`[channels.<name>]`
（见下）。别的顶层键会被忽略，但会在 stderr 上警告并点出名字
（`…/config.toml: unknown top-level keys ['modle']; ignored`），这样拼错了能看出来；它不算错误，免得
新旧版本的 noeta 各自认识的键让对方起不来。表里写了不认识的键则是错误。

### `[providers.<name>]` {#providers-name}

默认 provider 之外的更多 provider。一个模型 id 发给哪家：先看哪个表的 `models` 里写了它，再看哪家的
`/models` 列表里有它（默认 provider 先查），都没有就发给默认 provider。任务会记下它的模型走的哪条
路，之后每台 worker 都发到同一家（[provider 与模型路由](../how-it-works/models.md)）。

| 键 | 必填 | 含义 |
|---|---|---|
| `family` | 是 | `anthropic`、`openai_compat`、`openai_responses`，或第三方包提供的类型 |
| `base_url` | 否 | 同顶层的 `base_url`，按这个类型的写法 |
| `api_key` | 否 | 明文、`{env:NAME}` 或 `{keyring:service/user}`；不写时 `anthropic` 和 `openai_compat` 退回各自常用的环境变量 |
| `headers` | 否 | 请求头表；值可以是密钥引用 |
| `models` | 否 | 这家提供的模型 id，可以是字符串，也可以是 `{id, context_window, max_output_tokens}` 行；两个上限都写了的行，会把目录里没有的模型登记进去 |
| `list_models` | 否（默认 `true`） | 读这家的 `/models`，找更多模型和它们的上下文长度（在 `~/.noeta/cache/models` 缓存一天） |
| `prompt_cache_ttl` | 否 | 仅 `anthropic`：`"1h"` 或 `"5m"` |

写了别的键会报错。`default` 这个名字被占用了（指顶层那个 provider）。同一个模型写在两家名下也会
报错。顶层写 `provider = "<表名>"`，就让这个表成为默认 provider。

### 模型描述文件

`models_file` / `NOETA_MODELS_FILE` 指向一个 JSON 文件，描述内置模型目录里没有的模型，让它们按真实
上限运行：

```json
{"models": [
  {"id": "my-gateway/model-x", "context_window": 1000000, "max_output_tokens": 65536,
   "supports_vision": false, "family": "anthropic", "provider": "deepseek"}
]}
```

一行要有 `id`、`context_window`、`max_output_tokens` 才算数；`supports_vision`、`family`（接口
协议的类型）和 `efforts`（表示这是推理模型）可选；`provider` 按名字把这个模型分给某个已配置的
provider。直接写一个行的列表也可以。不写 `supports_vision` 时图片照发、由 provider 判断；纯文本
模型写 `false`。

模型的上限依次来自：noeta 的内置目录、模型描述文件或表里的行、provider 的模型列表，都没有就按
128,000 token 上下文和 16,384 token 输出处理，并给出警告。

### `[plugins.<name>]`

按插件的注册名写它的参数，对每个用到它的代理生效：

```toml
[plugins.shell]
background = true
[plugins.compaction]
compaction_model = "claude-haiku-5-5"
[plugins.web]
enabled = false                  # 对所有代理关掉
[plugins.my_plugin]              # 第三方插件的参数
endpoint = "https://…"
token = "{env:MY_PLUGIN_TOKEN}"
```

每个内置插件有哪些参数，见[内置插件](./plugins.md)。

### `[agents.<name>]`

只有一个键 `add_plugins`：在这个代理自己文件里的插件之后再加几个，写插件名或 `{name = {参数}}`。
`[agents."*"]` 对所有代理生效。代理已经有的插件不会重复加。

```toml
[agents.main]
add_plugins = ["my_plugin", { hooks = {} }]
[agents."*"]
add_plugins = ["audit_log"]
```

### `[channels.<name>]`

某个渠道的参数。渠道是长期监听外部对话（聊天工具、webhook）的组件，由 `noeta worker` 和
`noeta serve` 运行。`enabled = false` 表示不启用。

```toml
[channels.chat_bridge]
token = "{env:CHAT_TOKEN}"
```

### `[policy]`（只在管理员目录里）

只从机器管理目录的 `config.toml` 读（`/etc/noeta/config.toml`、
`/Library/Application Support/Noeta/config.toml`、`C:\Program Files\Noeta\config.toml`）：

| 键 | 含义 |
|---|---|
| `allowed_plugins` | 设了之后，noeta 自带以外的插件只加载这些 |
| `blocked_plugins` | 这些一律不加载，内置的也一样 |

## 插件设置 {#plugin-settings}

插件设置就是宿主对某个插件（按名字）说的话：它的参数（一个表，`enabled = false` 表示关掉）、一个
现成的 `Extension` 实例（在 Python 里），或者开 / 关。每一层都用同样的写法（`noeta/agents/agent_files.py`）：

1. `noeta` 命令根据你的目录推出来的参数（记忆的存放位置、给 hooks 和 MCP 的设置文件、技能目录、
   权限规则）；
2. Claude Code 插件目录自带的设置；
3. `config.toml` 的 `[plugins.<name>]`；
4. 前端自己传的（`-p` 的 `--mcp-config`、`--allowedTools` 等）。

后面的层按键覆盖前面的；实例或开 / 关则整体替换。代理文件里对某个插件写的参数
（`plugins: [{shell: {background: true}}]`）会叠在宿主给的参数之上。内置代理默认关掉 `memory` 和
`mcp`，要有设置点名它们才开；`noeta` 命令会把两个都打开。`[plugins.<name>]` 写了一个本机没装的插件，
启动时会提示一句；如果是从 noeta 里搬出去的插件，还会给出安装命令（`repetition_guard` 就是
`noeta plugin add repetition_guard`）。

参数里的密钥写成引用（`{env:NAME}`、`{keyring:service/user}`），创建插件时才解析；配置文件和任务
日志里只存引用。

除了 `enabled = false`，还能用任务配置项 `plugins_off`（一串插件名）只在一个任务里关掉插件，用
`~/.noeta/plugin-switches.json`（`{"off": [...]}`）让以后的新任务都不带它；这个文件由前端的插件菜单来写
（见[关掉插件](./plugins.md#switching-a-plugin-off)）。插件表里写 `switchable = false`，这两种都关不掉它：
请求和菜单都动不了，适合服务器必须留着的关卡类插件。`noeta plugin add <名字>` 去哪些插件市场里找，
记在 `~/.noeta/marketplaces.json`，由 `noeta plugin marketplace` 来改（见 [noeta 命令](./cli.md)）。

任务会记下它用了哪些插件（名字、来源、版本或文件哈希、参数的哈希）。缺了其中某个插件的 worker
绝不会在少了它的情况下跑这个任务（[插件体系](../how-it-works/plugins.md)）。

## 设置文件 {#settings-files}

JSON 设置文件（`~/.noeta/settings.json`、`.noeta/settings.json`、`.noeta/settings.local.json`、
`--settings`、管理员的 `managed-settings.json`）用 Claude Code 的格式。noeta 会用到的键
（`noeta/cli/settings_files.py` 以及读这些文件的插件）：

| 键 | 谁读 | 含义 |
|---|---|---|
| `env` | `noeta` 命令 | 在读其他任何东西之前设进 noeta 进程的环境变量 |
| `model` | `noeta` 命令 | 参数、环境变量、`config.toml` 都没指定时用的模型 |
| `agent` | 前端 | 新任务默认用的代理 |
| `outputStyle` | — | 会读，但不生效（noeta 不支持输出风格） |
| `language` | `environment` | 模型回答用的语言 |
| `attribution.commit`、`attribution.pr` | `environment` | 提交和 PR 末尾的署名行（`""` 表示不加）；`includeCoAuthoredBy: false` 会把两者都清空 |
| `permissions.allow`、`.deny`、`.ask` | `permissions` | 规则列表，Claude Code 的 `Tool(spec)` 写法；多个文件累加 |
| `permissions.defaultMode` | `permissions` | 起始权限模式 |
| `permissions.additionalDirectories` | `permissions`、`fs`、`skills` | 工具还能用的其他目录 |
| `permissions.disableBypassPermissionsMode` | `permissions` | `"disable"`：禁用 `bypassPermissions` |
| `useAutoModeDuringPlan` | `plan_mode` | 任何文件里写了 `false`，计划模式就不用 auto 模式的分类器 |
| `sandbox` | 本机执行环境 | Claude Code 的沙箱对象：`enabled`、`failIfUnavailable`、`filesystem`、`network`、`excludedCommands` 等（[命令在哪里执行](../guides/sandbox.md)） |
| `hooks` | `hooks` | Claude Code 的 hooks（[权限与 hooks](../guides/permissions.md)） |
| `mcpServers` | `mcp` | MCP 服务器（user 和 local 范围） |
| `enabledMcpjsonServers`、`disabledMcpjsonServers`、`enableAllProjectMcpServers` | `mcp` | 是否同意启动项目 `.noeta/mcp.json` 里的服务器（[MCP 服务器](../guides/mcp.md)） |
| `skillListingBudgetFraction`、`skillListingMaxDescChars` | `skills` | 技能清单的总长度预算和每个技能描述的长度上限 |
| `pluginConfigs` | Claude Code 插件 | `noeta plugin add` / `configure` 写入的 `userConfig` 值 |

## 环境变量

noeta 会读的变量（在 `noeta/` 和 `tui/` 里逐个查过）：

| 变量 | 谁读 | 含义 |
|---|---|---|
| `NOETA_HOME` | 全部 | noeta 的主目录（默认 `~/.noeta`）；`~/.agents/skills` 不跟着变，始终在用户主目录下 |
| `NOETA_MODEL` | `noeta` | 模型 |
| `NOETA_PROVIDER` | `noeta` | 默认 provider 的类型或表名 |
| `NOETA_BASE_URL` | `noeta` | 默认 provider 的地址 |
| `NOETA_API_KEY` | `noeta` | 默认 provider 的 key（不会传给 noeta 启动的命令） |
| `ANTHROPIC_API_KEY` | `anthropic` provider | 没配 key 时用它 |
| `OPENAI_API_KEY` | `openai_compat` provider | 没配 key 时用它 |
| `NOETA_STORE` | `noeta` | 存储地址（不会传给命令） |
| `NOETA_EFFORT` | `noeta` | effort |
| `NOETA_MODELS_FILE` | `noeta` | 模型描述文件 |
| `NOETA_SERVER_TOKEN` | `serve`、`--server`、`acp --server` | 服务端的 bearer token；客户端用它来发请求（不会传给命令） |
| `NOETA_AUTO_COMPACT_WINDOW` | `compaction` | 给自动压缩计算阈值时用的上下文长度设上限（即 Claude Code 的 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`） |
| `NOETA_AUTOCOMPACT_PCT_OVERRIDE` | `compaction` | 在有效上下文的这个百分比处压缩，如果这比默认阈值更早（即 Claude Code 的 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`） |
| `NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET` | `skills` | 技能清单的字符预算（即 Claude Code 的 `SLASH_COMMAND_TOOL_CHAR_BUDGET`） |
| `NOETA_MCP_TIMEOUT` | `mcp` | 除工具调用外每个 MCP 请求的超时，毫秒（默认 30 秒） |
| `NOETA_MCP_TOOL_TIMEOUT` | `mcp` | 服务器没设时，一次 MCP 工具调用的超时，毫秒（默认相当于不限） |
| `NOETA_MAX_MCP_OUTPUT_TOKENS` | `mcp` | MCP 结果超过这个大小就改存文件（默认 25,000） |
| `NOETA_MAX_MCP_DESCRIPTION_LENGTH` | `mcp` | MCP 工具描述和服务器说明的长度上限（默认 4,096 字符，延迟加载的工具加载后是 16,384；设了这个变量就两者都按它） |
| `NOETA_TERMINAL_MCP_TOOLS` | `mcp` | 逗号分隔的完整 MCP 工具名，它们返回结果后这一轮就结束 |
| `NOETA_MCP_OAUTH_CALLBACK_PORT` | `mcp` | OAuth 回调端口 |
| `NOETA_MCP_CLIENT_SECRET` | 带 `--client-secret` 的 `noeta mcp add` / `add-json` | OAuth 客户端密钥，省得手敲（不会传给命令） |
| `NOETA_WEB_SEARCH_API_KEY` | `web` | Tavily 的 key，设了才有 `WebSearch` 工具（不会传给命令） |
| `TZ` | 终端界面、`-p`、`acp` | 你的时区，随每个新任务作为 `timezone` 发出（没设就用 `/etc/localtime` 或 `/etc/timezone` 指的时区） |
| `VISUAL`、`EDITOR` | 终端界面 | `/memory` 和 `/config edit` 打开的编辑器 |

noeta 给它启动的东西设的变量：

| 变量 | 给谁 | 值 |
|---|---|---|
| `NOETA_PROJECT_DIR`、`CLAUDE_PROJECT_DIR` | hook 命令 | 任务的工作目录 |
| `NOETA_MCP_SERVER_NAME`、`NOETA_MCP_SERVER_URL` | MCP 服务器的 `headersHelper` | 服务器的名字和地址 |
| `CLAUDE_PLUGIN_OPTION_<KEY>` | Claude Code 插件的 hooks | 它的 `userConfig` 值 |

技能和命令正文里的 `${NOETA_SKILL_DIR}` / `${CLAUDE_SKILL_DIR}`、`${NOETA_PROJECT_DIR}` /
`${CLAUDE_PROJECT_DIR}`、`${NOETA_EFFORT}` / `${CLAUDE_EFFORT}`，以及 `${CLAUDE_SESSION_ID}`（根任务的
id）会被替换成实际的值。Claude Code 插件的文件里，`${CLAUDE_PLUGIN_ROOT}`、`${CLAUDE_PLUGIN_DATA}`
（`~/.noeta/plugin-data/<name>`）和 `${user_config.KEY}` 也会被替换。

noeta 配置里每个 `{env:NAME}` 引用到的变量，同样不会传给命令、hook 和 MCP 服务器；存储用 Postgres
时，`PGPASSWORD`、`PGPASSFILE`、`PGSERVICE`、`PGSERVICEFILE` 也一样（`noeta/toolkit/local_env.py`）。
其余的照常继承，和 Claude Code 一样。

桌面端读 `NOETA_DESKTOP_NOETA`（`noeta` 可执行文件的位置）、`NOETA_DESKTOP_WEB`（网页文件的位置）
和 `NOETA_DESKTOP_USER_DATA`，见[网页、桌面与编辑器](../guides/front-ends.md)。
