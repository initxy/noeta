# MCP 服务器

这一页讲怎样给 noeta 接上 MCP（Model Context Protocol）服务器、模型能看到它们的什么，以及服务器慢、挂了或者要登录时会发生什么。配置格式和 Claude Code 一样（`mcpServers`、`claude mcp …`），只是文件放在 noeta 自己的位置。

这些都由 `mcp` 插件（`noeta/plugins/mcp/`）负责。`noeta` 命令会自动打开它，读 settings 文件和项目里的 `.noeta/mcp.json`；自己用 SDK、通过 `default_agents` 建 agent 的宿主程序，要显式传 `plugins={"mcp": {...}}` 才会打开（见 `noeta/agents/__init__.py` 里的 `OFF_UNLESS_SET`）。

<Diagram name="guide2-mcp" />

## 添加服务器

`noeta mcp` 的用法和 `claude mcp` 一样（`noeta/cli/mcp_command.py`）：

```sh
# 本机进程（stdio）：直接给程序和参数，不经过 shell
noeta mcp add files -- npx -y @modelcontextprotocol/server-filesystem /srv/data

# 远程服务器，走 Streamable HTTP，带一个请求头
noeta mcp add -t http github https://api.example.com/mcp -H 'Authorization: Bearer ${GH_TOKEN}'

# 整条配置用 JSON 给
noeta mcp add-json -s user search '{"type":"sse","url":"https://search.example.com/sse"}'

noeta mcp list            # 列出所有服务器、所在范围，以及现在能不能连上
noeta mcp get github      # 看一个服务器的完整配置
noeta mcp remove github   # 同名出现在多个范围时加 -s 指定
```

`-e KEY=VALUE`（可重复）给 stdio 服务器设环境变量，`-H "Name: value"` 给远程服务器加请求头。这两个选项后面能跟好几个值，所以要放在名字和命令（或地址）后面，或者用 `--` 收尾：写成 `noeta mcp add -e A=1 files -- …` 的话，`files` 会被当成一个环境变量。`${VAR}` 记得用单引号括起来：这样文件里存的是引用，服务器启动时才从环境变量读出密钥，密钥本身不会写进文件。服务器名字可以带任意字符；用在工具名里的别名会把 `[A-Za-z0-9_-]` 以外的字符换成 `_`（`noeta/plugins/mcp/settings.py` 的 `alias_for`）。

### 范围：配置写到哪个文件

| `-s` | 文件 | 谁能用 |
|---|---|---|
| `local`（默认） | `<项目>/.noeta/settings.local.json` 的 `mcpServers` | 只有你，只在这个项目；项目被信任后才读 |
| `project` | `<项目>/.noeta/mcp.json` | 克隆仓库的每个人，但要先同意（见下文） |
| `user` | `~/.noeta/settings.json` 的 `mcpServers` | 只有你，所有项目 |

noeta 不读 `~/.claude.json`、项目根目录的 `.mcp.json`，也不读 `.claude/` 下的任何东西（[ADR-004](https://github.com/initxy/noeta/blob/main/docs/adr/004-noeta-reads-only-its-own-config-places.md)）。Claude Code 里的配置条目可以原样复制到上面这些文件里。

### 项目里的服务器要先信任、再同意

克隆下来的仓库不能自己启动进程。`.noeta/mcp.json` 里的服务器要同时满足两条才会启动：

1. 项目已被信任（在项目里执行 `noeta trust`）；
2. 项目自己的 settings 文件（`.noeta/settings.json` 或 `.noeta/settings.local.json`）用 Claude Code 的字段表示同意：`"enableAllProjectMcpServers": true`，或者把服务器名写进 `"enabledMcpjsonServers"`。

任何一个 settings 文件的 `"disabledMcpjsonServers"` 里有这个名字，它就不启动。写在你个人 settings 里的同意不算数：否则这个同意会跟着名字，带到每个用了同名服务器的仓库里（`noeta/plugins/mcp/settings.py`）。`noeta mcp list` 会把这样的服务器显示成 `pending approval (not started)`，工人也会在日志里写明哪些服务器在等同意。

```json
// <项目>/.noeta/settings.local.json
{ "enabledMcpjsonServers": ["files"] }
```

## 文件格式

所有位置都用 Claude Code 的 `mcpServers` 对象：

```json
{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${HOME}/data"],
      "env": { "LOG_LEVEL": "${LOG_LEVEL:-info}" }
    },
    "github": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "headers": { "Authorization": "Bearer ${GH_TOKEN}" },
      "timeout": 120000,
      "alwaysLoad": true
    }
  }
}
```

| 字段 | 含义 |
|---|---|
| `type` | `stdio`（有 `command` 时的默认值）、`http`（也可写 `streamable-http`；有 `url` 时的默认值）、`sse`（旧版 HTTP+SSE 组合）或 `ws`（WebSocket） |
| `command`、`args` | stdio 服务器的程序和参数，直接执行，不经过 shell |
| `env` | 叠加在 stdio 服务器继承来的环境变量之上 |
| `url` | 远程服务器的地址 |
| `headers` | 每个发往远程服务器的请求都带上 |
| `headersHelper` | 一条 shell 命令，建连接时运行，输出一个 JSON 对象作为请求头（最多 10 秒） |
| `oauth` | 只对 `http` / `sse`：`clientId`、`callbackPort`、`authServerMetadataUrl`（只接受 https）、`scopes` |
| `timeout` | 单次工具调用的时限，单位毫秒（小于 1000 的忽略，和 Claude Code 一致） |
| `alwaysLoad` | `true`：第一次请求就把这个服务器的工具 schema 发给模型；`false`：所有工具都延后加载，不管服务器自己怎么标 |
| `bareElicitationCapability` | 服务器不认 elicitation 的 `form` / `url` 模式时设 `true` |

`command`、`args`、`env`、`url`、`headers`、`headersHelper` 里的 `${VAR}` 和 `${VAR:-默认值}` 会用环境变量填上。读不了的条目会写日志并跳过，其他条目照常加载。

### 其他来源

- **agent 文件里的 `mcpServers`**（Claude Code 的字段）：只写名字表示引用别处配好的服务器；写成 `名字: {配置}` 则只给这个 agent 的任务加一个服务器（`noeta/agents/agent_files.py` 的 `plugin_overlays`）。
- **装在 `~/.noeta/plugins/` 的 Claude Code 插件**里的 `.mcp.json`（或它清单里的 `mcpServers`）。如果插件在项目的 `.noeta/plugins/` 里，它的服务器一律不启动，noeta 会提示你把它们复制到 `.noeta/mcp.json`，在那里走同意流程（`noeta/cli/claude_plugins.py`）。
- **`noeta -p --mcp-config 文件或JSON`**（可重复）：只给这一次无界面运行加服务器；加上 `--strict-mcp-config` 就只用这些（`noeta/cli/headless.py` 的 `mcp_arguments`）。
- **通过 ACP 接入的编辑器**：每段对话自己指定服务器，只放在内存里，不进日志（`noeta/host/acp.py` 的 `EditorServers`）。
- **宿主代码**：`Mcp(servers=...)` 接受 `McpServer` 列表、`mcpServers` 形状的字典，或者一个根据任务返回这两者之一的函数。

几个来源出现同一个别名时，先找到的生效，顺序是：agent 文件自己的服务器 → 代码、命令行和插件给的 → 项目的 `.noeta/mcp.json` → settings 文件，按 `~/.noeta/settings.json`、`.noeta/settings.json`、`.noeta/settings.local.json`、机器统一下发的 managed settings 的顺序（`noeta/plugins/mcp/extension.py` 的 `_resolve`，`settings.py` 的 `read_servers`）。一个名字最好只在一个地方配置。

## 模型看到什么

### 工具名

服务器的工具以 `mcp__<别名>__<工具名>` 的形式提供：工具名里 `[A-Za-z0-9_-]` 以外的字符换成 `_`，大小写不变，不截断。同一个服务器的两个工具名换完以后撞了，就各自加上 `_` 和原名 SHA-256 的前 8 位十六进制（原名本来就没有要替换的字符的，保持原样）。全名超过 128 个字符的工具不提供（服务器日志会说明，把服务器名改短就能解决）。两个服务器产生了同一个工具名算冲突：后一个会被当成连不上，直到改掉其中一个别名（`noeta/plugins/mcp/config.py`，`extension.py` 的 `refresh`）。

noeta 早期会把别名转小写（并截到 32 个字符）、把工具名截到 64 个字符。按旧名字写的权限规则、hook 匹配和 `--disallowedTools` 仍然认，但只用于收紧：旧名字在拒绝规则和询问规则里有效，在允许规则里无效（`noeta/core/mcp_names.py`）。

### 延后加载

MCP 工具默认延后加载（[ADR-017](https://github.com/initxy/noeta/blob/main/docs/adr/017-deferred-tool-loading-is-a-plugin.md)）：请求里只列出名字，模型要用某个工具时先调 `ToolSearch` 把它的 schema 加载进来，再去调用。这样一个有五十个工具的服务器，不会让每次请求都多带五十份 schema。想一开始就发 schema，在服务器上设 `"alwaysLoad": true`，或者由服务器在单个工具上标 `_meta["anthropic/alwaysLoad"]: true`。`_meta["anthropic/searchHint"]` 可以加一些让 `ToolSearch` 能搜到的词。`_meta.ui.visibility` 里没有 `"model"` 的工具不会提供给模型。

工具说明在只列名字时截到 4,096 个字符，加载后截到 16,384 个；设 `NOETA_MAX_MCP_DESCRIPTION_LENGTH` 会同时替换这两个上限。服务器的 `instructions` 按前一个上限截，在服务器第一次加入或说明有变化时，作为一条系统提示发给模型。

### 一轮之内工具不变

每轮开始时，noeta 向每个服务器要一次 `tools/list`，结果有变化时才在任务日志里记一条 `listed`。这一轮用的工具只从这条记录来，所以整轮的请求前缀（以及 provider 的缓存）保持不变。服务器发来 `list_changed` 通知时，在带来这条通知的调用结束后重新列一次，新工具从下一步起提供。因为工具集记在日志里，任务换到另一台工人上继续时，提供的工具完全一样，第一次调用时再重新连接。

这条记录只有别名和 schema。命令、地址、环境变量和请求头只放在工人内存里，从不进日志。

### 资源、提示词模板和 `/mcp` 命令

- 只要配置了服务器，主 agent 还会拿到 `ListMcpResourcesTool`、`ReadMcpResourceTool` 和 `ReadMcpResourceDirTool`。子代理只拿到服务器的工具，和 Claude Code 一样。
- 服务器的每个 prompt 都变成一条斜杠命令：`/mcp__<别名>__<prompt> 参数1 参数2…`（最后一个参数吃掉这一行剩下的内容）。
- `/mcp` 列出所有服务器的状态：已连接（带工具数）、等待登录、或失败。

## 上限与超时

| 项目 | 默认 | 修改方式 |
|---|---|---|
| `initialize`、各种列表、prompt、资源 | 30 秒 | `NOETA_MCP_TIMEOUT`（毫秒） |
| 单次 `tools/call` | 实际上不限（100,000 秒） | 服务器的 `timeout`（毫秒），没有就看 `NOETA_MCP_TOOL_TIMEOUT`（毫秒）；最终限制在 1 秒到 2,147,483 秒之间 |
| 工具结果 | 25,000 token | `NOETA_MAX_MCP_OUTPUT_TOKENS` |
| 工具说明 | 只列名字时 4,096 字符，加载后 16,384 | `NOETA_MAX_MCP_DESCRIPTION_LENGTH` |
| 工具名 | 128 字符 | 固定（Messages API 的上限） |
| 闲置连接 | 30 分钟没人用就关掉 | 宿主代码里 `McpPool(idle_ttl=…)` |

超出预算的纯文本结果会存到任务专门放超长输出的目录，模型只拿到文件位置和读取方法；带图片的结果保留放得下的部分，文字截断（`noeta/plugins/mcp/results.py`）。`NOETA_TERMINAL_MCP_TOOLS`（逗号分隔的完整工具名）里的工具调用成功后直接结束这一轮，对应 Claude Code 的 `CLAUDE_CODE_TERMINAL_MCP_TOOLS`。

## 出问题时会怎样

- **某个服务器在一轮开始时列不出工具**：这一轮先不用它。日志里记一条 `server_down`（前端会显示），模型也会收到一条说明，告诉它哪些服务器没连上，免得它以为这些功能不存在。下一轮再列一次；服务器挂着时，如果 `ToolSearch` 什么都没搜到，这一轮还会再列一次。
- **stdio 服务器进程死了，或 HTTP 服务端重启了**：只多一次重连。复用的连接在列表或读取请求上失败，就丢掉重连一次（`noeta/plugins/mcp/pool.py` 的 `use`）。连接遇到 EOF、管道断开或超长的一行，会被标记为坏连接，下次使用时重新启动服务器。
- **工具调用失败**：不重试，错误作为这次调用的结果交给模型。每次调用都在 `_meta` 里带上幂等键（`noeta/idempotencyKey`），支持去重的服务器可以据此丢掉重复请求。
- **调用进行到一半工人崩了**：模型收到“结果未知”，调用不会被重跑，因为 noeta 无法知道服务器是否已经执行了。
- **服务器反过来问人问题**（MCP 的 elicitation）：这次调用会挂起等人回答，问题和其他提问一样显示出来。因为这个未完成的调用活在当前工人进程里，等待期间这个工人一直保留对任务的认领。如果这个工人死了，下一个接手的工人会立即把这次调用答成“结果未知”。同一次调用里的第二个问题会被直接拒绝。
- **关闭时**：先关 stdio 服务器的 stdin，再结束、最后强杀它的整个进程组，它启动的子进程一起带走。它的 stderr 输出直接丢弃。

## 登录（OAuth）与密钥

远程 `http` 或 `sse` 服务器返回 401，就是需要登录。两种方式：

```sh
noeta mcp login github              # 打开浏览器，等待跳转回来
noeta mcp login --no-browser github # 只打印授权地址；授权后把浏览器地址栏里的网址粘回来
noeta mcp logout github
```

也可以在任务里登录：这个服务器的工具会被换成 `mcp__<别名>__authenticate` 和 `mcp__<别名>__complete_authentication` 两个。模型调用第一个，把授权地址给你；浏览器跳转不回来时，你把最后的地址粘给模型，它再交给第二个工具。登录成功后，服务器真正的工具取代这两个。5 分钟内没人完成登录，这次登录作废（`noeta/plugins/mcp/oauth.py`）。

noeta 按 MCP 授权规范来：先发现授权服务器，没设 `oauth.clientId` 时动态注册客户端，再走带 PKCE 的授权码流程。跳转地址是 `http://localhost:<端口>/callback`，端口依次取 `NOETA_MCP_OAUTH_CALLBACK_PORT`、`oauth.callbackPort`、随便一个空闲端口。令牌按服务器存在 `~/.noeta/mcp-auth`（目录 0700，文件 0600），过期前自动续期。

客户端密钥不会写进 settings 文件：`noeta mcp add --client-id ID --client-secret` 会提示你输入（或读 `NOETA_MCP_CLIENT_SECRET`），和登录信息存在一起。请求头、环境变量和令牌都不进任务日志。

stdio 服务器启动时，环境变量是 noeta 自己的环境去掉 noeta 的密钥（`NOETA_API_KEY`、`NOETA_STORE`、`NOETA_SERVER_TOKEN`、`NOETA_MCP_CLIENT_SECRET`、`NOETA_WEB_SEARCH_API_KEY`，以及 noeta 配置里以 `{env:NAME}` 交给插件或渠道的变量），再加上它自己的 `env`（`noeta/toolkit/local_env.py`）。

## 多台工人时的 MCP

- 每次工人接手一个任务，都会按自己的 `~/.noeta` 和任务的工作目录重新算出这个任务有哪些服务器。所以每台工人的配置要一样，否则任务换了机器，服务器也就变了。
- stdio 服务器跑在工人机器本身上，不在任务的执行环境或沙箱里，它看到的是工人机器的文件。
- 连接按工人进程共用：同一个范围（scope）里、用同一个服务器的任务共用一条连接。有状态的服务器（浏览器、登录态）因此可能把一个人的状态带进另一个人的任务；宿主代码可以用 `Mcp(scope=…)`（一个字符串，或根据任务返回字符串的函数）给每个人分开。
- 登录令牌存在每台机器自己的 `~/.noeta/mcp-auth`。在任务里发起的登录，在发起它的那台工人上进行。

## 相关页面

- [权限与 hooks](./permissions.md)：`mcp__github__*` 这类规则，以及匹配 MCP 工具的 hook。
- [内置工具](../reference/tools.md)：`ToolSearch` 和资源工具。
- [内置插件](../reference/plugins.md)：`mcp` 插件的参数。
- [作为服务端运行](./server.md)。
