# 作为服务端运行

这一页讲怎样把 noeta 跑成给别人和别的程序用的服务：`noeta serve` 提供 HTTP / WebSocket 接口，若干 `noeta worker` 进程负责跑任务，它们共用一个 Postgres 存储；另外还有访问令牌、网页端，以及让聊天应用接进来的渠道。各部分怎样在多台机器之间保持正确，见[多机运行](../how-it-works/many-machines.md)。现成的 Docker 镜像和 compose 文件见[用 Docker 部署](./docker.md)。

每个任务都是存储里的一条日志。工人挂了不会留下烂摊子：别的工人会从日志停下的地方认领它的任务接着跑。

<Diagram name="guide2-server-deploy" />

## 安装

服务端和 Postgres 是两个可选依赖（extra）：

```sh
uv tool install 'noeta[tui,server,postgres]'
```

`server` 带来 `noeta serve`（Starlette、uvicorn、websockets），`postgres` 带来 Postgres 存储（psycopg）。缺了哪个，相关命令会停下来并打印安装它的那一行（`noeta/cli/main.py` 的 `missing_extra`）。

## 单机

```sh
export NOETA_MODEL=claude-sonnet-5-5 NOETA_API_KEY=sk-ant-...
noeta serve                       # http://127.0.0.1:8642，同一进程里带一个工人
```

没设 `--store` / `NOETA_STORE` 时，存储是 `~/.noeta/tasks.db` 这个 sqlite 文件。不加 `--no-worker` 的 `noeta serve` 会在自己进程里跑一个工人（队列 `default`，同时跑 8 个任务），所以这样就是一个完整的服务端。第一次启动时它会生成访问令牌并打印一次（见[下文](#访问令牌)）。

## 多个工人

```sh
export NOETA_STORE=postgres://noeta:…@db.internal/noeta
noeta worker --concurrency 16            # 每台工人机器上都跑，多少台都行
noeta serve --no-worker --host 0.0.0.0 --allow-host api.internal   # 接口，可以放一台或几台机器
```

- **工人自己认领，没有人分配。** 工人用 `FOR UPDATE SKIP LOCKED` 拿走一个可认领的任务；租约（lease）规定工人不出声多久后就失去任务（`noeta worker --lease`，默认 30 秒）。每次写入都带着这次认领的序号（epoch）做检查，所以一个卡住后又醒过来的工人再也写不进去。
- **实时输出能跨机器。** 工人把模型正在生成的文字通过 Postgres 发出去（`noeta_deltas` 通知通道，尽力而为，不落库）；`noeta serve` 收到后在 `/tasks/{id}/watch` 上推给前端，所以连在任何一台服务端上的前端都能边生成边看到。用 sqlite（单机）时，实时输出只留在跑任务的那个进程里；已记录的事件仍然人人可见。
- **每台机器的配置一样。** 每个进程读自己的 `~/.noeta`（`NOETA_HOME`）：agent、插件、`config.toml`、settings 文件、MCP 服务器。把同一份配置发到每台机器上。如果某台工人缺了任务启动时用的插件，它绝不会在缺插件的情况下跑这个任务：它让租约自然过期，等有这个插件的工人来接；连续三次这样认领、日志里又没有新内容，任务以 `plugin_missing` 失败（见[写一个插件](./plugins.md)）。
- **队列。** 任务创建时指定一个队列（`POST /tasks` 的 `queue`，默认 `default`）；工人只拿它列出的队列（`noeta worker --queue NAME`，可重复）。可以用它把一些任务留在具备特定条件的机器上。
- **跳过审批模式。** 和 `noeta serve` 一样，工人默认拒绝 `bypassPermissions`，除非启动时加了 `--allow-bypass-permissions`。

### `noeta worker` 参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `--queue NAME` | `default` | 从哪个队列拿任务（可重复） |
| `--concurrency N` | 8 | 这个进程同时跑几个任务 |
| `--lease SECONDS` | 30 | 认领不续期能维持多久；工人挂掉后，它的任务大约等这么久才被别人接走 |
| `--snapshot-every EVENTS` | 100 | 最多每隔这么多个事件写一次任务快照（`0`：不写） |
| `--restart-limit N` | 3 | 任务反复把工人弄死时，最多连续重启这么多次（期间一步没做完），再崩就让它失败（`0`：不限） |
| `--allow-bypass-permissions` | 关 | 允许任务以 `bypassPermissions` 运行 |

### `noeta serve` 参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | 8642 | 端口 |
| `--no-worker` | 关 | 只提供接口，工人在别处跑 |
| `--no-token` | 关 | 不要访问令牌；只能用在本机回环地址上 |
| `--allow-host NAME` | — | 允许的 `Host` 请求头名字（可重复） |
| `--allow-origin ORIGIN` | — | 允许调用接口的网页来源（`https://host:port`，可重复） |
| `--allow-root PATH` | — | 除服务端自己的目录外，任务还能在哪些目录里跑（可重复） |
| `--allow-bypass-permissions` | 关 | 允许请求把任务启动成或切换到 `bypassPermissions` |
| `--web DIR` | — | 同时在其他路径上提供 `DIR` 里构建好的网页端 |

两者也接受通用参数（`--model`、`--store`、`--provider`、`--base-url`、`--cwd` 等），见 [noeta 命令](../reference/cli.md)。`--effort` 和 `--permission-mode` 在 `noeta serve` 上是客户端新建任务的起始配置（请求自己给了的以请求为准）；在 `noeta worker` 上只作用于它的渠道开的任务。两者给了 `--permission-mode bypassPermissions` 却没给 `--allow-bypass-permissions` 都会报错退出。接口列表见 [HTTP / WebSocket 接口](../reference/http-api.md)。

## Postgres

`NOETA_STORE=postgres://…`（或 `postgresql://…`，或 `--store`）就会用 Postgres 存储（`noeta/backends/stores/postgres.py`）。第一个连上的进程会建好 `noeta` 这个 schema 和里面的表，不需要别的准备。租约和认领时间用数据库的时钟，所以几台机器时钟不准也不影响对“谁持有任务”的判断。每次变化都在同一个事务里用 `pg_notify` 在 `noeta_changes` 上通知，等待中的任务收件箱一来东西就会被唤醒。

**升级。** 升级前先停掉所有工人（以及 `noeta serve`）；不同版本的工人不能共用一个存储。新版本的第一个进程会原地把存储的 schema 升上去。比存储 schema 旧的进程会拒绝打开它（`StoreUnavailable: … stop every worker and upgrade`）；0.6 版本不做这个检查，所以一定要先把旧工人全停掉。0.6 的存储（schema 版本 1）会被原地升到版本 5：`holds` 表删掉；它记着的后台任务已经随旧工人一起没了，相应任务在下一轮会被告知该后台任务已丢失。

**滚动重启。** `SIGTERM` 或 `SIGINT` 让工人停下：它正在跑的任务回到存储里，由别的工人按日志接着跑。正在等这台工人上某个后台任务的那些任务会继续等待，下一条消息到来时被告知后台任务已丢失。要上线新插件，就一台一台地停、装、再启动；要在几个租约周期内做完，因为启动时记下了新插件的任务如果一直落到没有这个插件的工人上，三次之后就会失败。

## 访问令牌

`noeta serve` 总是要求带 bearer 令牌（[ADR-009](https://github.com/initxy/noeta/blob/main/docs/adr/009-serve-requires-a-token.md)）：HTTP 接口创建的任务会以服务端的权限执行命令，而哪怕只监听本机回环地址，本机的其他进程和浏览器里的网页也都能访问到它。

- 令牌取自 `NOETA_SERVER_TOKEN`；没设的话第一次启动时生成一个，只打印这一次。
- `~/.noeta/server-token`（只有属主可读）里只存它的 SHA-256（[ADR-013](https://github.com/initxy/noeta/blob/main/docs/adr/013-server-token-kept-as-a-digest.md)），所以客户端（`noeta --server`、`noeta acp --server`）要在自己的 `NOETA_SERVER_TOKEN` 里放令牌。删掉这个文件再重启就会换一个新令牌。旧版 noeta 留下的明文令牌文件会在下次启动时改写成摘要，并最后打印一次令牌。
- `--no-token` 只能用在回环地址上。
- 绑定回环地址时，只回应回环地址的 `Host` 名（防 DNS 重绑定）。绑定其他地址时，只会对照 `--allow-host` 给出的名字检查 `Host`，所以要把名字写上；一个都没给的话，`noeta serve` 启动时会在 stderr 上提醒：任何 `Host` 都会应答。
- 来自其他来源网页的请求会被拒绝，除非用 `--allow-origin` 允许了这个来源。
- 在自己程序里嵌入这套接口时，用 `create_app(client, token=...)`（或 `token_sha256=`）；不给令牌它会拒绝创建，除非明确传 `auth=False`。

## 请求能设置什么

请求只能设置客户端被允许设置的任务配置（`noeta/host/_http_guard.py` 的 `REQUEST_CONFIG`）：`model`、`effort`、`max_turns`、`cwd`、`permission_mode`、`output_schema`、`timezone`、`interactive`、`permission_prompt_tool`、`ultracode`、`max_budget_usd`、`continued_as`、`bash_mode`。其他键一律拒绝（403）；`max_budget_usd` 不是大于 0 的数时返回 400，创建任务和配置信号都一样。请求只能发前端会发的那些信号，而且只能在服务端自己的目录和 `--allow-root` 目录里创建任务（相对路径的 `cwd` 以服务端目录为基准）。

- `timezone` 是用户的 IANA 时区（如 `Europe/Berlin`），noeta 自己的前端都会带上：不带的话，模型拿到的是 UTC 日期，并注明是 UTC，绝不会用服务器本地时区。定时任务（`CronCreate`）和 `ScheduleWakeup` 显示的时间也按它来；不带时按跑任务那台机器的时区。
- `output_schema`（根为 `"type": "object"` 的 JSON Schema）让模型通过 `StructuredOutput` 工具作答，对应 Claude Code 的 `--json-schema`；答案在任务的 `structured_output` `answer` 记录里（`noeta.toolkit.structured_output.structured_answer`）。
- `interactive: false` 表示没人实时盯着这个任务（不生成标题，除非设了 `permission_prompt_tool`，否则也没有 `AskUserQuestion`），和无界面协议的做法一样。这些键会一直保留，直到客户端重新设置；终端界面和 ACP 重新打开任务时会把它们去掉。
- `bash_mode: true` 表示客户端会替用户执行 `! <命令>`，就像终端界面在本机跑时那样：主提示词里就会多一行，让模型碰到交互式命令时建议用户这么跑。只在创建任务时设；子代理不会带上它（fork 出来的任务沿用父任务的提示词，所以会带上）。
- 配置信号只能删除、不能设置 `client_hooks`，这是无界面客户端的 hook 回调记录（`noeta.toolkit.hook_callbacks`）。
- `ultracode: true` 是 Claude Code 的 ultracode 开关：告诉模型遇到像样的任务都用 `Workflow` 来编排，关掉时也会再告诉它一次（`noeta.plugins.workflow.opt_in`）。
- `continued_as` 说明一个用 `history_from` 创建的任务以什么方式接续原任务，供 SessionStart hook 判断：`fork`（默认）、`resume` 或 `rewind`（不触发任何 hook）。

模型走哪个 provider 由服务端记录，客户端不能指定。

## 前端

- **终端界面**：`noeta --server http://api.internal:8642`。
- **编辑器**（ACP）：`noeta acp --server URL`。
- **网页端**：`noeta serve` 直接在 `/` 提供安装包里自带的那份（`--web DIR` 可以换成别的构建）。页面会问一次令牌，或者从地址的 `#token=…` 片段里取（片段不会发到服务端，只存在浏览器里）。网页文件本身不需要令牌就能访问，带有内容安全策略：只允许服务端自己的脚本、样式和请求，不允许被嵌入别的页面。
- **你自己的程序**：用 `noeta.RemoteClient` 或直接发 HTTP；`noeta.connect(url)` 给存储地址或服务端地址都能返回一个客户端。

用 `--server` 连上的前端需要在 `NOETA_SERVER_TOKEN` 里放令牌，自己不需要配模型：新任务用服务端的模型、在服务端的目录里跑，除非 `--model` 指定了模型，或 `--cwd PATH` 指定了服务端上的某个目录。`noeta -p` 总是在自己进程里跑任务，不接受 `--server`。各个前端的细节见[网页、桌面与编辑器](./front-ends.md)。

## 命令在哪里执行

工具在任务 `cwd` 上的执行环境（exec env）里读写文件、执行命令。任务每走一步都可能换到另一台机器上继续，所以这个目录在每台工人上看起来必须一样：要么是在每台工人上挂到同一路径的共享文件系统（默认的本机执行环境直接可用），要么是远程执行环境，比如容器，由宿主代码按任务选择（`Runtime(env_for=…)`）。`noeta` 命令本身总是用本机执行环境，settings 打开沙箱时再套上沙箱。

任务自己的文件也是一样：存下来的长工具输出（`~/.noeta/tool-results/<任务 id>/`）、计划文件（`~/.noeta/plans/`）和自动记忆（`~/.noeta/projects/<项目>/memory/`、`~/.noeta/agent-memory/`）。用本机执行环境加共享文件系统时，`NOETA_HOME` 也要放到共享挂载上（或者把这四个目录链接过去），并且每台工人上路径相同。无论哪种方式，下面这些仍然只在一台工人本地：后台 `Bash` 任务和它的日志（后台任务运行期间，所属任务一直保留在这台工人上；工人挂了，任务会被告知后台任务已丢失），以及用户粘贴的图片（接手的工人会根据对话重新写出来）。

细节，以及怎样在本机、沙箱和容器之间选择，见[命令在哪里执行](./sandbox.md)。

## 渠道：让聊天应用接进来

渠道（channel）是一个长期运行的监听程序，用来接外部对话，比如一个聊天群或一个 webhook（`noeta/core/channel.py`）。`noeta worker` 和 `noeta serve` 会运行 `config.toml` 里启用的每个渠道，各自在后台跑；某个渠道出错会记日志，5 秒后重新启动（`noeta/host/channels.py`）。

```toml
# ~/.noeta/config.toml
[channels.chat_bridge]
secret = "{env:CHAT_BRIDGE_SECRET}"
allow_senders = ["U123"]

[plugins.chat_reply]
url = "https://chat.example/hooks/{env:CHAT_HOOK_TOKEN}"
```

这是官方插件市场里的 [`chat-bridge`](https://github.com/initxy/noeta-plugins/tree/main/chat-bridge) 渠道（`noeta plugin add chat-bridge` 装上）：聊天应用把每条消息连同共享密钥发到 8788 端口的 `POST /chat`，每个聊天串对应一个任务，任务里的 `chat_reply` 插件把回答发回去。[`chat-approvals`](https://github.com/initxy/noeta-plugins/tree/main/chat-approvals) 在此基础上加了“允许 / 拒绝”按钮，用来审批任务正在等的工具调用（8789 端口）。

渠道跑多个副本时怎样保持正确：

- `deliver()` 不管几个副本同时收到，都只为一个聊天串创建一次任务，每条消息按投递 id 只投一次。聊天串比任务活得久：任务结束后，下一条消息会基于之前的对话开一个接续任务 `<id>.2`、`<id>.3`……
- 外部发送者的话总是带着 `<channel source="…" sender="…">` 包装送进来，模型不会把它当成任务主人说的话。
- 回复从任务内部发出（插件的 `on_event`），任务只有一个写者，所以只会发一次。发送要按事件的序号做幂等，就像 chat-bridge 用 `Idempotency-Key` 请求头那样。

怎样写自己的渠道，见[写一个插件](./plugins.md)。

## 清理

- **存下来的工具输出。** 带自己工人的 `noeta serve`、`noeta worker` 和本机前端，每天清理一次存放超长输出的目录（`~/.noeta/tool-results`；`noeta/host/spill_sweep.py`，由 `LocalApp.start` 启动）。`noeta serve --no-worker` 不做这项清理；它的任务由各个工人执行，输出也写在工人那台机器上，由工人自己清理。一个任务（及其整棵任务树）结束、并且它的目录 30 天没有写入，就删掉，这是 Claude Code 保留工具结果的时长。还在等待中的任务，不管等多久，输出都保留。清理只管本机的目录；任务跑在远程执行环境里的宿主，要自己清理那个环境里的目录。
- **快照。** 任务结束时删除它的快照。
- **任务日志**从不删除：任务就是发生过什么的记录。前端可以把任务归档（`archive` 信号），归档后不再出现在列表里。

## 多方共用

一个存储加一个服务端只服务一方：谁有令牌谁就能操作所有任务，工具以工人的操作系统用户身份运行（[ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)）。要服务彼此不能互相看到的几方，就给每一方单独的服务端和单独的数据库（或单独的机器）。`noeta` 命令总是用 `noeta` 这个 Postgres schema；宿主代码可以传 `PostgresStore(dsn, schema=…)`。也可以在宿主代码里用扩展点自己做隔离：每方单独的队列和工人池（`noeta worker --queue NAME`）、每个任务单独的执行环境（`Runtime(env_for=…)`，比如容器）、用 `Mcp(scope=…)` 分开 MCP 连接，再加一个 `Extension` 做关卡、限额和计费。
