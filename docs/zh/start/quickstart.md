# 快速上手

从零开始，五分钟跑起一个能干活的 agent：安装 noeta，接上模型，用一下终端界面和 `noeta -p`，
再学会怎么回到之前的对话。

## 1. 安装

需要 Python 3.11 或更新版本，以及 [uv](https://docs.astral.sh/uv/)。从 PyPI 装 `noeta`，连同终端界面：

```bash
uv tool install 'noeta[tui]'      # 或者：pip install 'noeta[tui]'
```

或者从本地仓库装：

```bash
git clone https://github.com/initxy/noeta && cd noeta
uv tool install '.[tui]'
```

装完跑一下 `noeta --version` 确认。

终端界面是单独的包 `noeta-tui`（在 `tui/` 下），和 noeta 一起发布，版本号永远相同，`tui` 这个
extra 会装上对应的版本。不装它，`noeta` 照样能跑 `-p`、`serve`、`worker`、`acp`，直接敲 `noeta`
时会告诉你怎么补装。其他可选组件：

| 组件（extra） | 用途 |
|---|---|
| `tui` | 终端界面（`noeta-tui`） |
| `server` | `noeta serve`，HTTP / WebSocket 接口 |
| `acp` | `noeta acp`，给 Zed 等编辑器用的 Agent Client Protocol |
| `postgres` | Postgres 存储，多台 worker 时用 |
| `keyring` | 配置里的 `{keyring:service/user}` 密钥引用 |
| `workflow` | `Workflow` 工具跑脚本用的引擎（`mini-racer`，内嵌的 V8，装好约 80 MB） |

一次装多个：`uv tool install 'noeta[tui,acp,server,postgres]'`。
注意 `uv tool install --reinstall` 只保留这次写出来的 extra 和 `--with` 包，所以每次都要写全。
某个命令缺组件时，noeta 会说缺哪个，并打印安装命令。

**升级**直接跑 `noeta update`：它先去 PyPI 看有没有新版，有就按你当初装的 extra 和 `--with` 包重装
最新版（pip 装的就跑 `pip install -U`；源码目录它不动，交给你自己更新）。已经是最新时会说
`noeta is up to date`。

## 2. 选模型和 provider

noeta 需要知道用哪个模型、怎么连上它。最快的办法是设环境变量。

**Anthropic 的 API：**

```bash
export NOETA_MODEL=claude-sonnet-5-5
export NOETA_API_KEY=sk-ant-...          # 或者 ANTHROPIC_API_KEY
```

**兼容 OpenAI 的接口**（vLLM、各种网关、DeepSeek 等）：还要写上 provider 和地址。

```bash
export NOETA_PROVIDER=openai_compat
export NOETA_BASE_URL=https://llm.example.com/v1
export NOETA_MODEL=deepseek-flash
export NOETA_API_KEY=...                 # 或者 OPENAI_API_KEY
```

不同类型的 provider，地址写法不一样：

| `NOETA_PROVIDER` | `NOETA_BASE_URL` | noeta 实际请求的地址 |
|---|---|---|
| `anthropic`（默认） | 只写主机，例如 `https://api.anthropic.com` | `<base>/v1/messages` |
| `openai_compat` | 以 `/v1` 结尾 | `<base>/chat/completions` |
| `openai_responses` | 完整的 `…/v1/responses` 地址 | 就是这个地址 |

同样的设置也可以用命令行参数（`--model`、`--provider`、`--base-url`），或者写进
`~/.noeta/config.toml`。定下来之后写进文件更省事：

```toml
# ~/.noeta/config.toml
model = "claude-sonnet-5-5"
provider = "anthropic"
# key：环境变量 NOETA_API_KEY 或 ANTHROPIC_API_KEY，
# 或者在这里写 api_key：明文 "sk-ant-..."，或者 "{env:MY_KEY}" 这样的引用
```

命令行参数优先于环境变量，环境变量优先于 `config.toml`（[配置](./configuration.md)）。同时配置多个
provider、目录里没有的模型、`opus` / `sonnet` / `haiku` 这些别名，见[模型与 provider](../guides/models.md)。

内置模型目录里没有的模型也能跑，只是按保守的 128,000 token 上下文和 16,384 token 输出上限来，
启动时会提示。在 `[providers.<name>]` 表或模型描述文件里写上它真实的上限就好
（[配置项](../reference/config.md)）。

## 3. 第一次打开终端界面

```bash
cd ~/code/some-project
noeta
```

第一次在某个目录里打开时，noeta 会问你信不信任这个目录。选"是"等于对这个目录执行了
`noeta trust`：从此它自己的 `.noeta/` 设置、代理、技能、提示词和插件才会加载。选"否"就退出。
（也可以先自己跑一下 `noeta trust`。）

然后像在 Claude Code 里一样直接说你要干什么：

```text
> 这个仓库是做什么的？测试怎么跑？
```

头一天值得知道的几件事：

- **权限模式。** 新任务默认是 `auto`。Shift+Tab 在几种模式间切换（`default` 改东西前先问、
  `acceptEdits`、`plan`、`auto`、`bypassPermissions`）；`--permission-mode` 指定起始模式。需要
  确认的操作会弹出卡片，直接在界面里回答（[权限与 hooks](../guides/permissions.md)）。
- **斜杠命令。** `/help` 列出全部；`/model`、`/effort`、`/context`、`/cost`、`/compact`、
  `/rewind`、`/resume` 等见 [noeta 命令](../reference/cli.md#slash-commands)。
- **停下和离开。** Esc 停止当前这一轮；`/exit` 退出。任务一直留在存储里。

noeta 保存的东西都在 `~/.noeta/`（或 `$NOETA_HOME`）下：存储文件 `~/.noeta/tasks.db`、设置、信任
列表、插件。这个目录创建时只有你自己能读；如果它或存储文件对其他用户开放，noeta 会提醒你。

## 4. 在脚本里跑一句：`-p`

`-p` 不开界面，跑一个提示词并打印回答，和 `claude -p` 一样：

```bash
noeta -p "这个仓库是做什么的？"
echo "总结一下 README" | noeta -p          # 提示词从标准输入读
noeta -p --output-format json "列出所有 TODO"  # 输出一个 JSON 结果对象
```

JSON 结果里有回答（`result`）、`is_error`、`num_turns`、`total_cost_usd`、`usage` 和按模型
分的用量，字段和 Claude Code 的一样。加 `--json-schema '<schema>'` 可以拿到结构化输出，
`--max-turns` / `--max-budget-usd` 用来给一次运行设上限。

给程序用的 stream-json 协议也和 Claude Code 的一样，所以把 Agent SDK 指向 `noeta` 就能驱动它：

```bash
noeta -p --output-format stream-json --input-format stream-json --verbose
```

和 Claude Code 的无界面模式一样，`-p` 下凡是需要问人的操作都会被拒绝，除非客户端负责回答
（`--permission-prompt-tool stdio`）。所以要选好权限模式，比如 `--permission-mode acceptEdits`，
或者用 `--allowedTools "Bash(git *) Edit"` 放行具体的工具。全部参数见 [noeta 命令](../reference/cli.md)。

## 5. 接着聊，或者回到某次对话

每次对话都是存储里的一个任务，退出、重启、崩溃之后都还在。

```bash
noeta -c                 # 接着本目录里最近的一次对话
noeta -r                 # 从列表里挑一个（终端界面）
noeta -r <任务 id>        # 打开指定任务
noeta -p -c "那就先修第一个 TODO"                        # -p 也能用 -c
noeta -p -r <任务 id> --fork-session "换个办法试试"       # 从那次对话分出一个新任务
```

在终端界面里，`/resume` 列出以前的对话；`/rewind`（或连按两次 Esc）回到你某次提问之前，可以选择
恢复对话、恢复文件，或两者都恢复。

## 6. 网页版

装了 `server` 这个 extra（`uv tool install 'noeta[tui,server]'`）之后，`noeta serve` 会起一个带网页版的本机服务，
并打印要打开的地址，令牌已经放在地址里了：

```text
$ noeta serve
noeta: web app at http://127.0.0.1:8642/#token=…
```

在浏览器里打开它：左边一列是你的项目，每个项目下面是它的会话；不选项目就开的会话，在自己的临时目录里跑。
想关掉终端后服务还在、登录时自动起来，就把它装成系统服务：`noeta service install`
（见[作为服务端运行](../guides/server.md#让它一直跑着-noeta-service)）。

## 7. 一段话讲清信任

克隆下来的仓库不能自己改你的 agent 配置，也不能自己启动进程。所以项目里的
`.noeta/settings.json`、`.noeta/settings.local.json`、`.noeta/agents/`、`.noeta/skills/`、
`.agents/skills/`、`.noeta/prompts/`、`.noeta/plugins/` 都要等 `noeta trust`（或第一次打开时选
"是"）之后才加载。信任过的目录记在 `~/.noeta/trusted.json`。即便信任了，项目 `.noeta/mcp.json`
里的 MCP 服务器也要在项目自己的设置文件里点头（`enabledMcpjsonServers` 或
`enableAllProjectMcpServers`）才会启动。`NOETA.md` / `AGENTS.md` 无论信不信任都会读，就像
Claude Code 读 `CLAUDE.md` 一样（[配置](./configuration.md#trust)）。

## 接下来

- [配置](./configuration.md)：每个设置从哪来，谁说了算。
- [终端界面与 -p](../guides/terminal.md)：界面的按键、无界面协议。
- [作为服务端运行](../guides/server.md)：worker、`noeta serve`、`noeta --server`。
- [在 Python 里嵌入](./embedding.md)：在你自己的程序里用同一个内核。
