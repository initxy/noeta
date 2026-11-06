# 排错

实际使用中常见的出错情况：noeta 会打出什么话，原因是什么，该改哪里。本页引用的报错原文都抄自代码，
拿其中一句去仓库里搜，就能找到出处。

## 先看哪里

- **命令自己的报错。** `noeta` 命令遇到能说清的问题（没配模型、没有 key、参数不允许）时，会打一行
  `noeta:` 开头的话然后退出。先读这一行。
- **任务这一轮失败的原因。** 一轮跑不下去时，日志里会记一条 `TurnFailed`，带上原因。交互式任务保留对话，
  等下一条消息；一次性任务（所有子代理，以及代理文件里写了 `one_shot: true` 的）直接以
  `TaskEnded(status="failed", error=…)` 结束，`error` 以原因开头。原因列表见下文「一轮失败的原因」。
- **工人的标准错误输出。** noeta 自己不配置日志，用的是 Python 默认行为：`noeta worker`、`noeta serve`
  的警告和错误都写到标准错误。用服务管理器跑它们，并把这部分输出留下来。
- **`noeta plugins`** 列出每个插件和它的来源；加载失败的插件文件标 `(failed)`，后面跟原因。
- **终端界面里的 `/mcp`** 列出每个 MCP 服务器的状态：已连接、等登录、失败（带报错）。

## 启动时

| 看到的报错 | 原因 | 怎么办 |
|---|---|---|
| `noeta: no model configured (--model or NOETA_MODEL)` | 没有任何设置指定模型。 | 加 `--model`，或设 `NOETA_MODEL`，或在 `~/.noeta/config.toml` 里写 `model`（见[配置](../start/configuration.md)）。 |
| `noeta: no API key for the anthropic provider: set NOETA_API_KEY (ANTHROPIC_API_KEY is read too), or api_key in ~/.noeta/config.toml` | 默认 provider 没有 key。 | 设报错里提到的任一环境变量，或写 `api_key`（明文，或 `{env:NAME}` / `{keyring:service/user}` 引用）。 |
| `…/config.toml: unknown top-level keys ['modle']; ignored` | 顶层有 noeta 不认识的键，多半是拼错了；它本来要设的那项还是没设上。 | 改对键名（见[配置项](../reference/config.md#顶层键)）。 |
| `no API key for provider 'x': set api_key in its [providers.x] table of …/config.toml` | 某个 `[providers.<name>]` 表里没 key。 | 在那张表里加 `api_key`（可以写成 `{env:NAME}` 或 `{keyring:service/user}` 引用）。 |
| `…: {env:NAME} names an environment variable that is not set`、`…: the keyring has no secret for 'service/user'`、`…: {keyring:…} needs the keyring package` | 这台机器上填不出某个密钥引用。引用只在插件或 provider 真正创建时才去读，所以第一次用到时才报。 | 设好变量、把密钥存进系统钥匙串（`keyring set service user`），或装上 `keyring` 这个可选依赖。 |
| `noeta: this needs the server extra, which is not installed: pip install '…'`（也可能是 `postgres`、`acp`、`keyring`） | `noeta serve`、Postgres 存储、`noeta acp`、钥匙串密钥各自需要一个可选依赖组。 | 照打出来的那行装。用 `uv tool install --reinstall` 时要把你在用的可选依赖全写上，重装会丢掉没写的。 |
| `noeta: the terminal UI is its own package, noeta-tui, which is not installed: …` | 不带子命令的 `noeta` 会启动终端界面，而终端界面是单独的 `noeta-tui` 包。 | 照打出来的那行装；只想问一句的话用 `noeta -p`。 |

## 模型与 provider

noeta 把各家 provider 的错误归成几类（`noeta/core/errors.py`），值得重试的会自动重试。每次重试都记进日志，
前端显示为「正在重试」。

| 类别 | 什么时候 | noeta 怎么处理 |
|---|---|---|
| `Transient` | 网络错误、超时、HTTP 5xx（包括 Anthropic 的 529 过载）、流式回复开始后断掉 | 退避重试：从 1 秒起翻倍，最多 30 秒，其中一半是随机的；总共最多 9 次（`DEFAULT_MAX_ATTEMPTS`，`noeta/core/retry.py`）。断掉的流可能改成整段请求再发一次。 |
| `RateLimited` | HTTP 429 | 和 `Transient` 一样重试；provider 给了 `Retry-After` 就正好等那么久。 |
| `ContextOverflow` | 413，或者 4xx 的正文说提示太长 | 不原样重试。如果 provider 说输入放得下、只是输出上限放不下，就调小输出上限再发；否则交给压缩腾地方（见下）。 |
| `Fatal` | 其他 4xx：key 不对、请求格式不对、模型拒答 | 不重试。这一轮以 `llm_error` 失败，带上 provider 的原话，正文截到 500 个字符。 |

重试用完后，这一轮以 `llm_error: <message> — <body>` 失败。重点看 body，那是 provider 自己说的话。

下面几条其实是配置问题，不是 provider 的问题：

- `provider 'x' cannot be used: …`：`[providers.x]` 这张表建不出适配器（没 key、某个设置不对）。改那张表。
- `this task runs 'model' on provider 'x', which is not configured here (providers: …)`：配了多个 provider
  时，任务会把「这个模型发给哪个 provider」记进自己的配置（`model_route`），之后每台工人都照这个发。
  这台工人的 `config.toml` 里没有这个名字的表。让所有工人的 `[providers.*]` 保持一致。
- `request carries an image but model 'm' is catalogued with supports_vision=False; refusing to send the
  image to a model that cannot read it.`：模型目录里写着这个模型看不了图。这条消息换个模型发。
- `this provider does not support image input outside a plain user turn; refusing to silently drop the
  image.`：OpenAI 兼容接口在这个位置放不了图片。
- `the model call kept being interrupted`（原因是 `llm_error`）：同一次模型调用开始了三次都没等到回复记进日志
  （`MAX_MODEL_ATTEMPTS`），也就是每次调用中途工人都死了。去查工人为什么会死（内存、被管理器杀掉等）。

模型目录里没有的模型，按 128,000 token 窗口、16,384 token 输出上限处理，并打一条警告。如果网关上的模型
压缩触发得太早或太晚，在模型文件或 provider 的表里写上它真实的上限（见[模型与 provider](../guides/models.md)）。

## 上下文超长与压缩

请求放不进窗口时，压缩插件会把对话做成摘要，然后重新调用。只有这条路也走不通时你才会看到失败：

| 原因 | 报错 | 怎么办 |
|---|---|---|
| `context_overflow` | provider 原本的超长提示 | 没有插件能腾出空间（压缩关了，或者这个代理没带压缩插件）。打开压缩，或者开个新任务。 |
| `autocompact_thrashing` | `Automatic compaction stopped: the context filled up again within 3 model calls of the previous compaction, 3 times in a row. A file or tool output is probably too large for the context window; read it in smaller parts, or start a new conversation.` | 某个文件或某段输出几乎占满了整个窗口。让模型分段读。 |
| `compaction_summary_failed` | 摘要调用失败的原因 | 做摘要的那次调用本身失败了（摘要请求太长时最多重试三次）。自动压缩连续失败三次后，这个任务就不再自动压缩，`/compact` 仍然能用。 |
| `compaction_config_invalid` | 不合法的那个值 | `autoCompactWindow` 或 `NOETA_AUTOCOMPACT_PCT_OVERRIDE` 对这个模型不可用。改代理文件或设置。 |

原理见[上下文、缓存与压缩](../how-it-works/context.md)。

## 一轮失败的原因

一轮会以哪些原因失败（`noeta/core/runner/texts.py` 以及会抛 `TurnFailed` 的插件）。前端会显示出来，
宿主程序按前缀判断。

| 原因 | 含义 |
|---|---|
| `llm_error` | provider 重试后仍失败，或直接拒绝了请求。 |
| `llm_empty_response` | 模型既没给文字也没调工具。 |
| `llm_truncated` | `the response kept hitting the output token limit`：连续三次撞到输出上限，续写也救不回来。调高模型的输出上限，或让它每步少做一点。 |
| `context_overflow`、`autocompact_thrashing`、`compaction_summary_failed`、`compaction_config_invalid` | 见上一节。 |
| `max_turns` | `Reached maximum number of turns (n)`：到了任务的 `max_turns`（`--max-turns`）。 |
| `needs_human` | `the sub-agent stopped without completing (it needed an interaction that is unavailable to a background agent)`：宿主把任务标成了无人值守（`unattended`），它却走到了一个审批或提问。给它配好覆盖所需操作的权限规则，或者别标无人值守。 |
| `blocked_by_hook`、`stopped_by_hook` | `UserPromptSubmit` hook 拦下了这条输入，或者某个 hook 停掉了这一轮。详情是 hook 给的理由。 |
| `max_structured_output_retries` | 设了输出 schema，但模型在重试上限内没交出合格的 `StructuredOutput` 答案。 |
| `internal_error` | runner、插件 hook 或 provider 适配器里的 bug。连续两次就结束任务。工人的标准错误里有调用栈。 |

任务到了步数上限不算失败：模型会收到 `You have reached the limit of {n} steps for this task…`，最后的回答以
`Stopped at the {n}-step limit before finishing.` 开头。

## 工具调用说「结果未知」

```
Interrupted before its result was recorded, so its outcome is unknown — it may have fully,
partly, or not applied. Check its effect before running it again.
```

跑这个调用的工人在记下 `ToolStarted` 之后、记下结果之前死了（或丢了认领）。noeta 不会自作主张再跑一遍，
除非这个工具能自己查出结果（`Tool.recover`），或者声明了重跑无害（`retry_safe`）。模型读到这段话，一般会先去核实。
除非工人经常死，否则不需要处理（见[单写者与崩溃恢复](../how-it-works/recovery.md)）。

后台 `Bash` 任务所在的工人死掉时，模型会收到 `Background command "…" was lost: the worker process
running it stopped, so its further output and exit status are unknown`（`Monitor` 则是
`Monitor "…" was lost: …`）。后台命令只活在一个工人进程里，没法重新接上。

## 插件

| 看到的报错 | 原因 | 怎么办 |
|---|---|---|
| `(failed) <path>: <error>; install it next to noeta: uv tool install --reinstall --with <module> '…'`（来自 `noeta plugins` 或 `/reload-plugins`） | 插件文件 import 了 noeta 所在环境里没有的模块。`noeta plugin add` 故意不装任何依赖。 | 照打出来的那行装，并把你已经在用的可选依赖和 `--with` 都写上。 |
| `plugin file <path>: 'name' joins no agent; set add_to = ("main",) on its class, or [agents.main] add_plugins = ["name"] in config.toml` | 插件加载了，但没有哪个代理用它。 | 照提示二选一（见[写一个插件](../guides/plugins.md)）。 |
| `<project>/.noeta/plugins was not loaded: this project is not trusted (run `noeta trust` in <project> to load its plugins)` | 项目里的 `.noeta/plugins`、`.noeta/agents`、`.noeta/prompts`、`.noeta/skills`、`.agents/skills` 只有在你信任这个项目之后才加载。 | 在项目目录里跑 `noeta trust`，或者在终端界面里回答信任提问。 |
| 插件一直不出现，也没有报错 | 机器级的 `/etc/noeta/config.toml` 里 `[policy]` 把它列进了 `blocked_plugins`，或者设了 `allowed_plugins` 却没写它（这个名单只管 noeta 自带插件以外的）。这份策略压过所有用户和项目设置。 | 找管这台机器的人。 |
| 任务以 `plugin_missing: this task runs with <names>, which the worker that claimed it does not have; install them there (or run the task where they are)` 结束 | 见下。 | 每台工人都装上这个插件。 |

### 工人缺了任务要用的插件

任务开始时会把自己用的插件记进日志（`records.Plugins`）。缺其中任何一个的工人都不会在缺插件的情况下跑它：
它什么都不写，打一条 `task …: runs with …, which this worker does not have; leaving it to a worker that has
them`，然后等租约到期，让别的工人来认领。如果同一台工人连续三次（`LOAD_ATTEMPTS`）认领到它，而日志一直没有新内容，
就以 `plugin_missing` 把任务结束为失败。

<Diagram name="ops-plugin-missing" />

滚动安装拖得太久，或者漏装了一台机器，就会出现这种情况。给每台工人发同一套插件（以及同一份 `~/.noeta`），
见[作为服务端运行](../guides/server.md)。这个计数存在每台工人的内存里，工人重启后从头数。

如果连续三次认领都读不出任务的日志或从日志算不出状态，任务以
`unreadable_log: the task's log could not be read on 3 claims in a row; last error: …` 结束。
更新版本的 noeta 写的日志不算在内：旧工人会把它留给新工人。

## 存储

| 看到的报错 | 原因 | 怎么办 |
|---|---|---|
| `schema 'noeta' is at version N; this noeta knows M (stop every worker and upgrade)`（Postgres），`sqlite store <path> is at schema version N; …` | 更新版本的 noeta 已经把存储升级了，旧进程拒绝打开。 | 把这台机器也升级。不同版本的工人不能共用一个存储：全部停掉、升级、再启动。 |
| `cannot open the postgres store: …`、`no postgres connection: …`、`postgres connection lost: …` | 连不上 Postgres。 | 检查 `NOETA_STORE` 和网络。什么都没写进去，可以重来。 |

如果连接恰好在提交写入时断开，这次写入可能成功了也可能没成功（`CommitUnknown`）。工人会把这个任务当成丢了认领，
停止运行；下一个认领它的工人会去读日志。你可能看到任务停顿一个租约时长（默认 30 秒）。

## 服务端和客户端

| 看到的报错 | 原因 | 怎么办 |
|---|---|---|
| `noeta: the server at <url> needs its bearer token: set NOETA_SERVER_TOKEN to the token noeta serve printed when it made it` | 客户端没有令牌。 | 在客户端设 `NOETA_SERVER_TOKEN`。 |
| `noeta: the server at <url> refused the token: …`，或 HTTP 401 `{"error": "unauthorized"}` | 令牌不对。服务端在 `~/.noeta/server-token` 里只存 SHA-256，没法再显示一次原文。 | 找出 `noeta serve` 第一次打印时你存下的令牌；或者删掉 `~/.noeta/server-token` 重启服务端，拿一个新的（只打印这一次）。 |
| HTTP 403 `{"error": "cross-site request refused"}` | 请求的 `Host` 不在服务端应答的名单里（绑定回环地址时只认回环名字；绑别的地址时，只要给过 `--allow-host` 就只认这些名字），或者是别的源的网页发来的浏览器请求。 | 用 `--allow-host NAME` 加上客户端用的主机名；页面放在别处的话用 `--allow-origin ORIGIN`。 |
| `noeta: --no-token serves only a loopback address` | `--no-token` 配了一个非回环的 `--host`。 | 保留令牌，或者绑到 `127.0.0.1`。 |
| `this server does not allow bypassPermissions (start it with --allow-bypass-permissions)` | `noeta serve` 和 `noeta worker` 默认拒绝 bypass 模式。任务运行中切换时，会收到通知 `bypassPermissions is disabled here`。 | 确实需要的话，每台工人和服务端都带上 `--allow-bypass-permissions` 启动。 |
| `noeta: --permission-mode bypassPermissions needs --allow-bypass-permissions` | 启动 `noeta serve` 或 `noeta worker` 时给了 bypass 模式，却没允许它。 | 加上 `--allow-bypass-permissions`，或者换个模式。 |
| `noeta: on 0.0.0.0 any Host header is answered; name the names this server is reached by with --allow-host (DNS-rebinding protection)` | `noeta serve` 绑在非回环地址上，又没给 `--allow-host`。这只是提醒：服务照常运行，token 照样起保护作用。 | 客户端用哪些名字访问，就逐个用 `--allow-host NAME` 写上。 |
| `POST /tasks/{id}/answers` 返回 HTTP 409 `task … is not waiting on a request '…' (unknown, or already answered)` | `request_id` 打错了、来自过期的卡片、是别的任务的，或者已经答过并被取走。 | 重新读一遍 `GET /tasks/{id}/pending`，回答上面列着的。 |
| `/stream-json` 的 WebSocket 以关闭码 `4401` / `4403` 断开 | `4401`：没有有效 token。`4403`：要的权限模式或 `cwd` 被拒，关闭原因里写着为什么。 | 带上 token（`?token=` 或请求头）；换成允许的模式或目录。 |
| `cwd '…' is outside this server's directories (…); start it there or add the directory with --allow-root` | 客户端要在服务端不负责的目录里建任务。 | 给 `noeta serve` 加 `--allow-root PATH`。 |
| `a request may not set the task config key 'x' (only model, effort, …)` | 客户端只能设一部分配置项，其余的会放松权限关卡。 | 把这一项写进服务端自己的配置或代理文件。 |
| `the request failed on the server; its log has the details` | 出错的不是 noeta 自己的代码。原文可能带服务器路径，所以不发给客户端。 | 去看服务端的标准错误。 |
| `noeta: -p runs its task here; --server attaches the terminal UI (or noeta acp --server) to a server` | `-p` 总是在本机跑。 | 去掉 `--server`，或改用终端界面、`noeta acp --server`。 |
| `noeta: --plugin-dir, … apply to the tasks this process runs; with --server the server's settings and plugins run` | 远端任务用的是服务端的插件、权限规则、设置和 MCP 服务器。 | 在服务端配好，或者去掉 `--server`。 |

跨机器看不到实时输出、但记录的事件都在：实时输出走 Postgres 通知（`<schema>_deltas`），尽力而为，不落库。
回复开始之后才连上的前端，等回复记进日志时会看到完整内容。用 sqlite 时，实时输出只留在跑任务的那个进程里。

## MCP 服务器

`/mcp` 列出每个服务器：`✔ name · connected · N tools`、`△ name · needs sign-in` 或
`✘ name · failed: <error>`。命令行里用 `noeta mcp list` 检查。

| 看到的现象 | 原因 | 怎么办 |
|---|---|---|
| `noeta mcp list` 显示 ``project not trusted (not started; run `noeta trust` here)`` | 这是还没被信任的项目里的 `local` 或 `project` 服务器；连检查都不会把它启动。 | 在项目里执行 `noeta trust`。 |
| 项目 `.noeta/mcp.json` 里的服务器一直不启动 | 项目里的服务器只有在项目受信任、并且项目自己的设置里表示同意之后才启动。`noeta mcp add` 会提示：`It starts once you consent: add 'name' to enabledMcpjsonServers in …`。 | 先 `noeta trust`，再在 `.noeta/settings.local.json` 的 `enabledMcpjsonServers` 里加上名字（或设 `enableAllProjectMcpServers: true`）。 |
| `MCP tool name 'x' is produced by both server 'a' and server 'b'; rename one alias` | 两个服务器给出了同名工具。 | 在配置里给其中一个服务器改名。 |
| `MCP server 'x' needs exactly one of command and url` | 配置里 `command` 和 `url` 都写了或都没写。 | 改配置。 |
| `spawn failed: …` | stdio 服务器的命令起不来。 | 检查工人上的 `command` 和 `PATH`。 |
| 模型收到 `The 'x' MCP server needs a sign-in. …`，`/mcp` 显示 `△ x · needs sign-in` | HTTP 服务器要 OAuth 登录。 | `noeta mcp login x`，或者打开模型给你的链接。 |
| 请求里没有某个服务器的工具 | MCP 工具默认延迟加载：模型需要时用 `ToolSearch` 载入。名字超过 128 个字符的工具永远不提供（每次列工具时记一条日志）。 | 在服务器上设 `alwaysLoad: true`，从第一个请求起就带上它的工具。 |
| 调用或列工具超时 | 列工具、prompts、resources 的时限是 30 秒（`NOETA_MCP_TIMEOUT`，毫秒）；工具调用实际上不限时，除非服务器的 `timeout`（毫秒）或 `NOETA_MCP_TOOL_TIMEOUT` 设了。 | 设服务器的 `timeout` 或这个环境变量。 |
| 用 `default_agents` 的 SDK 宿主里一个 MCP 工具都没有 | 内置代理在没有对应设置前，MCP、记忆和重复检测都是关的（`OFF_UNLESS_SET`，`noeta/agents/__init__.py`）。`noeta` 命令会替你设好。 | 给 `mcp` 传一个设置（`plugins={"mcp": {...}}`）。 |

更多见 [MCP 服务器](../guides/mcp.md)。

## 沙箱

| 看到的报错 | 原因 | 怎么办 |
|---|---|---|
| `sandbox.enabled is set but the sandbox cannot start: bubblewrap (bwrap) is not installed; install it (apt install bubblewrap, dnf install bubblewrap) to run commands in the sandbox. Commands run without it.` | Linux 上没装 `bwrap`。设了 `failIfUnavailable` 时结尾是 `Commands that would run in it are refused (failIfUnavailable).` | 装上 bubblewrap。 |
| `… bwrap cannot create the sandbox here: …` | 有 `bwrap`，但建不了命名空间：在没开用户命名空间的容器里，或者内核设置禁止。 | 允许非特权用户命名空间，或者别在那个容器里跑 noeta。 |
| `… the sandbox's system-call filter (refusing Unix sockets and new user namespaces) does not support the <arch> architecture` | 系统调用过滤只认 x86_64 和 aarch64。 | 同时设 `sandbox.network.allowAllUnixSockets` 和 `sandbox.allowNestedUserNamespaces`（这样就用不着过滤），或者在那台机器上关掉沙箱。 |
| 模型收到 `the sandbox is required (sandbox.failIfUnavailable) but cannot start: <reason>. The command was not run, and running it outside the sandbox (dangerouslyDisableSandbox) is refused too.` | 开了 `failIfUnavailable`，但沙箱起不来。 | 按上面的原因修。 |
| `the sandbox does not allow connecting to host:port; add it to sandbox.network.allowedDomains to watch this socket` | `Monitor` 要连的 WebSocket 主机不在沙箱允许范围里。 | 把主机加进 `sandbox.network.allowedDomains`。 |

没开 `failIfUnavailable` 时，沙箱起不来命令就不受限地跑，只在启动时给一条上面那样的通知。见[命令在哪里执行](../guides/sandbox.md)。

## 某些工具没出现

- **`WebSearch`** 要有搜索后端的 key（`NOETA_WEB_SEARCH_API_KEY`，Tavily）才提供。
- **`Workflow`** 在 `mini-racer` 包内嵌的 V8 里跑脚本。没有它时工具会拒绝，报
  `the mini-racer package is not installed (…)` 或 `mini-racer's V8 library does not load here (…)`，什么都不启动。
- **任务清单工具**（`TaskCreate` 等）只提供给 Claude Code 也会给的那些模型，除非设了 `[plugins.todos] all_models = true`。
