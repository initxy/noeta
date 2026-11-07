# HTTP / WebSocket 接口

`noeta serve` 提供的每个接口、怎么认证，以及两种流式格式：`/tasks/{id}/watch` 的事件流，和
`/stream-json` 上 Claude Code 的无界面协议。代码在 `noeta/host/http.py`、`_http_routes.py`、
`_http_plugins.py`、`_http_stream.py`、`_http_guard.py`；`GET /openapi.json` 以 OpenAPI 3.1 文档的形式返回同一套接口。
怎么把服务端跑起来，见[作为服务端运行](../guides/server.md)。

这套接口就是客户端接口搬到网络上：创建、发消息、回答、发信号、取消、读取、跟踪。
`noeta.RemoteClient`（以及 `noeta.connect("https://…")`）用和 `noeta.Client` 相同的方法调用它；
终端界面（`noeta --server URL`）、`noeta acp --server URL`、网页和桌面端用的都是它。

## 认证，以及每个请求都要过的检查

<Diagram name="ref-request-guard" />

1. **Host 和来源。** 绑在回环地址上时，只认回环名字（`127.0.0.1`、`localhost`、`::1`）和
   `--allow-host` 给的名字；绑在其他地址上时，只有给了 `--allow-host` 才检查 `Host`。浏览器从别的
   来源发来的请求会被拒绝，除非来源就是服务端自己的页面，或者在 `--allow-origin` 里。被拒时返回
   `403 {"error": "cross-site request refused"}`。
2. **Bearer token。** 每个请求都要带 `Authorization: Bearer <token>`。WebSocket 可以改用
   `?token=`，因为浏览器在那里设不了请求头；别的路由都不认这个查询参数，服务端也会把 `?token=`
   的值从日志里抹掉。没带或不对时返回 `401 {"error": "unauthorized"}`。`noeta serve --no-token`
   （只限回环地址）可以关掉这一步。
3. **请求能要求什么**（`GuardedClient`）。创建任务时，请求只能设这些配置项：`model`、`effort`、
   `max_turns`、`cwd`、`permission_mode`、`output_schema`、`timezone`、`interactive`、
   `permission_prompt_tool`、`ultracode`、`max_budget_usd`、`continued_as`、`bash_mode`、`plugins_off`。`max_budget_usd`
   必须是大于 0 的数（布尔值、字符串都不行），这里和 `config` 信号里都一样，不合规就返回 `400`。
   `plugins_off` 是一串插件名，不能包含任务离不开的内置插件（内置插件里只能关 `web`、`workflow`、
   `schedule`、`todos`、`findings`、`title`、`quiet_nudge`、`memory`、`mcp`、`checkpoints`、
   `skills`、`ask_user`），否则返回 `403`。服务端
   启动时带的 `--effort`、`--permission-mode` 垫在请求的配置下面，请求自己给了的以请求为准。只能发前端会发的那几种信号：
   `permission_mode`、`config`、`resumed`、`restore_files`、`rename`、`archive`。除非服务端带了
   `--allow-bypass-permissions`，否则不能让任务以 `bypassPermissions` 启动或切过去。`cwd` 必须在
   服务端的目录或某个 `--allow-root` 之内（相对路径按服务端的目录解析，并以绝对路径存下）；没给
   `cwd` 的任务就在服务端的目录里跑。被拒时返回 `403` 和原因。

其他错误：任务不存在返回 `404 {"error": "unknown task …"}`；请求体或查询参数有误返回 `400` 和原因。
所有错误的响应体都是 JSON `{"error": …}`；不在本页列出的路径返回普通的 `404`（除非服务端还托管着
网页文件，见下文）。

token 取自 `NOETA_SERVER_TOKEN`；没有的话，服务端第一次启动时生成一个，只打印一次；
`~/.noeta/server-token` 只存它的 SHA-256
（[ADR-013](https://github.com/initxy/noeta/blob/main/docs/adr/013-server-token-kept-as-a-digest.md)）。
在自己的代码里构建这个应用时，调用 `noeta.host.http.create_app(client, token=…)` 或
`token_sha256=…`；不要 token 只能显式传 `auth=False`。

## 任务

| 方法与路径 | 请求体 / 查询参数 | 返回 |
|---|---|---|
| `POST /tasks` | `{agent, input?, config?, queue?, dedupe_key?, history_from?, history_through?}` | `201 {"task_id": …}` |
| `GET /tasks` | `?parent_id=&status=&limit=&roots_only=`（`status`：`ready`、`running`、`waiting`、`ended`；`limit` 默认 100；`roots_only=true` 只列没有父任务的任务） | 任务行的列表 |
| `GET /tasks/{id}` | | 一个任务行 |
| `GET /summaries` | `?limit=&archived=`（`limit` 默认 50；`archived=true` / `false` 只要归档的 / 只要没归档的） | 根任务列表，最近更新的在前，每项 `{task_id, agent, status, waiting_on, end_status, created_at, updated_at, title, cwd, prompt, archived}`（`prompt` 是第一次提问，前 200 字符） |
| `GET /tasks/{id}/events` | `?after=N` | 序号 `N` 之后的事件，每个是 `Recorded`（`seq`、`at`、`event`） |
| `GET /tasks/{id}/watch` | `?after=N` | `text/event-stream`，见下 |

- `input` 是字符串，或一条用户消息 `{"role": "user", "content": [...]}`，里面只能有文字和图片块；
  它总是算作人说的话（客户端不能以别的角色或来源写进对话）。
- `dedupe_key` 让创建幂等：重复请求返回第一次的任务 id。
- `history_from` / `history_through` 让新任务从另一个任务的对话出发，截到那个任务的第
  `history_through` 个事件（即分叉）。
- 任务行包括 `task_id`、`root_id`、`parent_id`、`agent`、`queue`、`status`、`waiting_on`、
  `end_status`、`last_seq`、`created_at`、`updated_at`、`ready_at`、认领信息 `owner`、
  `owner_epoch`、`lease_until`，以及收件箱计数 `inbox_seq` / `inbox_read_seq`。还是默认值
  （`null`、`0`）的字段不会出现在 JSON 里。
- 十种事件及其字段见[事件](./events.md)。

## 和任务交互

| 方法与路径 | 请求体 | 返回 |
|---|---|---|
| `POST /tasks/{id}/messages` | `{input, when?, dedupe_key?}`（`when`：默认 `next_turn`，或 `now`） | `{"inbox_seq": n}`；如果是本地斜杠命令，返回 `{"output": 文字}`，不发送任何东西 |
| `GET /tasks/{id}/pending` | | 等人处理的审批和提问，包括子代理的 |
| `POST /tasks/{id}/answers` | `{request_id, decision?, always?, data?}` | `{"ok": true, "inbox_seq": n}`；任务没在等这个 `request_id` 时返回 `409` |
| `POST /tasks/{id}/signals` | `{name, data?, dedupe_key?, wake?}` | `{"ok": true, "inbox_seq": n}` |
| `POST /tasks/{id}/cancel` | `{reason?, turn_only?}` 或不带请求体 | `{"ok": true}` |

**消息。** 以 `/name args` 开头的文字会先执行任务所用代理里同名的斜杠命令：提示词类命令展开后的
文字才是真正发出去的；本地命令直接返回 `{output}`。对不上任何命令的文字原样发出。

**待处理项。** 每一项有 `task_id`（提问的那个任务，可能是子代理）、`request_id`、`kind`
（`approval` 或 `question`）、`tool`、`input`、`prompt`、`detail`、`agent`，一个用于卡片的
`title`（如"Bash command · from the Explore agent"），审批还有 `choices`（`[{id, label}]`：同意、
"以后不再问"的规则、切换模式、拒绝）。

**回答。** 审批的 `decision` 是 `allow` 或 `deny`，`always: true` 会同时记住卡片上给的那条规则。
`data` 可以补充：`{"mode": m}` 选卡片上的模式选项，`{"message": t}` 给拒绝附上理由或给同意附上话。
提问的 `data` 把每个问题 id 映射到 `{"selected": [选项]}` 或 `{"other": "文字"}`。任务没在等的
`request_id`（不存在，或者已经答过、答复已被取走）返回 `409`，免得打错的 id 或过期的卡片被悄悄
“答掉”；答复被取走之前请求一直算在等，所以重试照样成功，同一个请求的第二次回答会被忽略。

**信号。** `permission_mode` 带 `{mode}` 切换权限模式；`config` 带 `model`、`effort`、
`max_turns`、`output_schema`、`interactive`、`permission_prompt_tool`、`ultracode`、
`max_budget_usd`、`plugins_off` 中的任意几个，修改任务配置（值为 `null` 表示删掉这个键；请求只能删除
`client_hooks`，不能设置它；`max_budget_usd` 必须是大于 0 的数）；`resumed`、`restore_files`、`rename` `{title}`、`archive`
`{archived}` 是前端在重新打开、回退、改名、归档时发的。`wake: false` 让正在等待的任务继续等。
模型走哪家 provider 由服务端记录，从不采用请求里给的。

**取消。** 结束任务；带 `turn_only: true` 时只停下当前这一轮。

**终端界面等前端开的对话。** 终端界面、`noeta -p` 或编辑器开的对话在 `front-end` 队列上，服务端的
worker 不会自己去取。只要通过这里给它发消息、回答、取消、发一个会唤醒它的 signal，或者回退，
服务端就把这整棵任务树接过来：挪到服务端自己的队列上，从此在这里跑（之后前端再打开它也照样能跑）。
如果某个前端正在跑它（或者在等待期间还占着认领），就什么都不挪：发来的东西交给那个前端去处理。
回退出来的新任务总是建在服务端的队列上。

## 查看任务的更多信息

| 方法与路径 | 返回 |
|---|---|
| `GET /tasks/{id}/usage` | 整个任务树的 `tasks`、`cost_usd`、`api_seconds`、`usage`，以及按模型分的 `by_model`（每个模型的 `usage`、`calls`、`cost_usd`、`api_seconds`） |
| `GET /tasks/{id}/context` | 下一次请求里装了什么：`{model, window, total, categories, deferred_tools, tools}`，其中 `categories` 是各部分估算的 token 数（`System prompt`、`System tools`、`MCP tools`、`Messages`、`Free space`，顺序同 Claude Code 的 `/context`）。只有服务端进程里有 worker 在跑这个任务的代理时才有（`--no-worker` 时没有），否则返回 `501` |
| `GET /tasks/{id}/rewind` | 你的每一次提问，每项 `{index, task_id, seq, at, text, files}`（之后改过的文件） |
| `POST /tasks/{id}/rewind` | `{index, conversation?, files?}`（两者默认都为 true）：回到第 `index` 次提问之前，恢复对话（在新任务里）、文件，或两者都恢复。返回 `{task_id, prompt, files}` |

## 命令、代理、模型、文件

| 方法与路径 | 查询参数 / 请求体 | 返回 |
|---|---|---|
| `GET /commands` | `?agent=&task_id=&cwd=` | 某个代理的插件提供的斜杠命令：`[{name, description, argument_hint, local}]` |
| `POST /commands/run` | `{text, agent?, task_id?, cwd?}` | 对 `/name args` 返回 `{text, local}`，没有这个命令时返回 `null`；什么都不发送 |
| `GET /agents` | | `[{name, description, model, one_shot}]` |
| `GET /models` | | 模型目录：`[{id, context_window, max_output_tokens, aliases}]` |
| `GET /files` | `?q=&task_id=&cwd=&limit=` | 任务目录（或 `cwd`，都没有就是服务端的目录）下路径里含 `q` 的文件，给提示词里的 `@` 补全用（`limit` 默认 30） |
| `GET /openapi.json` | | 这套接口的 OpenAPI 3.1 文档 |

带 `task_id` 时，命令在那个任务自己的代理和目录里列出、执行；带 `cwd` 时，这个目录会像新任务的
目录一样被检查。

## 插件

| 方法与路径 | 查询参数 / 请求体 | 返回 |
|---|---|---|
| `GET /plugins` | `?agent=&cwd=`（`agent` 默认 `main`） | `{"plugins": [PluginInfo], "ui_api": [最小, 最大]}` |
| `PUT /plugins/{name}` | `{enabled}` | `{"ok": true}`：这个插件在之后新建的任务里开还是关 |
| `GET /tasks/{id}/ui` | | `{"items": [UiEntry]}`：任务的插件此刻要显示的东西 |
| `POST /plugins/{name}/call` | `{task_id, name, args?}` | `{"result": …}`，即插件 `ui_call` 的回答；任务没在用这个插件、或插件不认这个调用时返回 `404` |
| `GET /plugins/{name}/web/{path}` | | 插件网页部分的文件，**不要 token** |
| `GET /market` | | `{"entries": [MarketEntry]}`：已配置的插件市场里有哪些插件 |
| `POST /plugins/install` | `{source, marketplace?, commit?}` | `201`，返回装好的插件的 `PluginInfo` |
| `DELETE /plugins/{name}` | | `{"ok": true}` |

- `PluginInfo` 是 `{name, version, description, source, builtin, enabled, switchable, broken,
  parts, ui_api, web}`。`enabled`：在这个智能体里，并且新任务默认开着；`switchable`：前端可以给它
  开关（装上的插件，或上面 `plugins_off` 那句列出的内置插件）；`broken`：它本身或它的网页部分为什么
  用不了，没问题时为 `null`；`parts` 取自 `backend`、`ui`、`web`、`skills`、`agents`、`commands`、
  `hooks`、`mcp`；`web` 是 `{"entry": "/plugins/<name>/web/<file>?v=<hash>"}` 或 `null`。列表先列
  智能体自己的插件，再列装了但它没用上的、加载失败的插件文件，最后是 Claude Code 插件。
- 单个任务的开关是带 `plugins_off` 的 `config` 信号；`PUT /plugins/{name}` 只改这台机器上新任务
  的默认值（存在 `~/.noeta/plugin-switches.json`，每个新任务创建时并进它的 `plugins_off`）。任务
  离不开的插件返回 `400`；`--no-worker` 时开关由跑任务的那边管，返回 `501`。
- `UiEntry` 是 `{plugin, type, key, …}`：`type` 为 `status` 时带 `text` 和 `tone`，为 `widget` 时
  带 `markdown` 和 `placement`，为 `notice` 时带 `text` 和 `level`。前端在跟踪的日志往前走时再取一次。
  `--no-worker` 时总是空的。
- 网页文件只为这里某个智能体在用、并且清单里 `ui_api` 在服务端支持范围内的插件提供，只从它 `web`
  模块所在的目录里取，出不了这个目录；响应头和网页应用的一样（JavaScript 文件是
  `text/javascript`）。Host 和来源检查照样做。
- `MarketEntry` 是 `{marketplace, name, description, version, source, commit, parts, ui_api,
  installed}`。
- 安装和删除做的就是 `noeta plugin add` 和 `noeta plugin remove` 做的事，做完像 `/reload-plugins`
  一样重新加载插件。只有服务端监听在回环地址上（桌面端就是这样）、或启动时带了
  `--allow-plugin-install` 才允许，否则返回 `403`。需要填选项（`userConfig`）的插件不能通过 HTTP
  装（`400`），请在终端里装。

## 事件流：`GET /tasks/{id}/watch`

<Diagram name="ref-http-stream" />

一条服务端推送事件流（`text/event-stream`，`cache-control: no-cache`），内容是 `?after=N` 之后的事件：

```text
event: event
data: {"seq":42,"at":1793872072.95,"event":{"type":"ModelCalled","model":"claude-sonnet-5-5",…}}

event: delta
data: {"kind":"text","text":"Reading the","call_seq":42,"attempt":1,…}

: keep-alive

event: end
data: {}
```

- `event` 消息是已记录的事件，按顺序，从 `after` 开始每个只发一次。
- `delta` 消息是不入库的实时输出：模型文字（`kind: "text"`）、思考过程（`thinking`），或正在跑的
  工具最新的输出（`tool`，带 `call_id`、`tool`、`elapsed`）。`call_seq` 和 `attempt` 指明片段属于
  哪次 `ModelCalled`；重试会从头再来，所以要丢掉旧尝试的片段。以记录下来的 `ModelReplied` 为准。
  实时输出来自服务端进程里的 worker；用 Postgres 存储时，也通过 Postgres 通知收到其他机器上 worker
  的输出（尽力送达）。用 sqlite 又加了 `--no-worker` 时就没有。
- 15 秒没有消息时发一行注释 `: keep-alive`。
- 任务结束且事件都已发完时发 `end` 并关闭；跟踪任务出错时 `end` 的内容是 `{"error": …}`。

连接断了就用最后看到的 `seq` 作为 `after` 重新连。`RemoteClient.watch` 会自动重连（中间没收到事件
的话，最多连续重连五次）。

## Claude Code 的无界面协议：`/stream-json`

和 `noeta -p --input-format stream-json --output-format stream-json` 是同一个协议
（`noeta/host/stream_json.py`），所以给 Claude Code Agent SDK 写的程序可以驱动远端的 noeta。

**WebSocket** `GET /stream-json`（升级连接）：每帧一条 JSON 消息，双向。客户端发 `user` 消息和
`control_request`；服务端发 `system` / `init`、`assistant` 和 `user` 消息、开启部分消息时的
`stream_event`、系统通知、每个提示词一个 `result`，以及它自己的 `control_request`（`can_use_tool`：
客户端在这条连接上回答权限确认）。查询参数：`resume`（任务 id）、`fork`、`agent`、`model`、`cwd`、
`permission_mode`、`max_turns`、`include_partial_messages`、`replay_user_messages`，以及 `token`。
别的网站的页面发来的、或者 `Host` 被重绑定的请求，在握手时就被拒绝（`HTTP 403`），根本连不上。
本服务的客户端被拒时，先接受连接，再用它读得到的关闭码关掉：没有有效 token 是 `4401`（原因
`unauthorized`），服务端不允许的模式或 `cwd` 是 `4403`，原因里写明为什么（最多 123 字节）。
`resume` 的任务不存在时发一条 `{"type": "error", …}` 后关闭。

**POST** `/stream-json`：同样的协议，单向。请求体是逐行的 JSON 输入，响应是逐行的 JSON 输出
（`application/x-ndjson`），直到活干完。POST 上没人能回答权限确认，所以凡是要问人的都直接拒绝，
这一轮继续。服务端不允许的模式或 `cwd` 在开跑前就返回 `403`。

```bash
printf '%s\n' '{"type":"user","message":{"role":"user","content":"这个目录里有什么？"}}' |
  curl -sN -X POST "http://127.0.0.1:8642/stream-json?permission_mode=default" \
       -H "Authorization: Bearer $NOETA_SERVER_TOKEN" --data-binary @-
```

控制请求和消息类型与 `noeta -p` 处理的一样（[noeta 命令](./cli.md#headless-noeta-p)）。

## 网页文件

用 `noeta serve --web DIR`（构建好的网页，`web/dist`）时，上面没列出的路径都返回这个目录里的文件，
不需要 token：这些文件里本来就没有 token。它们带着内容安全策略（页面的脚本、样式和请求只能来自
本服务端；图片还允许 `data:` 和 `blob:`）、`X-Frame-Options: DENY` 和
`X-Content-Type-Options: nosniff`。页面会问一次 token，或者从地址里的 `#token=…` 取，这一段不会
发给服务端。

## 示例

```bash
export NOETA_SERVER_TOKEN=…        # 服务端打印出来的 token
H="Authorization: Bearer $NOETA_SERVER_TOKEN"
S=http://127.0.0.1:8642

# 创建任务
curl -s -X POST $S/tasks -H "$H" -H 'content-type: application/json' \
  -d '{"agent":"main","input":"列出所有 TODO","config":{"permission_mode":"default"}}'
# → {"task_id":"40efba61…"}

# 跟踪它
curl -sN $S/tasks/40efba61…/watch -H "$H"

# 回答它在等的事
curl -s $S/tasks/40efba61…/pending -H "$H"
curl -s -X POST $S/tasks/40efba61…/answers -H "$H" -H 'content-type: application/json' \
  -d '{"request_id":"toolu_…","decision":"allow"}'

# 再说一句，然后停下这一轮
curl -s -X POST $S/tasks/40efba61…/messages -H "$H" -H 'content-type: application/json' \
  -d '{"input":"先修第一个"}'
curl -s -X POST $S/tasks/40efba61…/cancel -H "$H" -H 'content-type: application/json' \
  -d '{"turn_only":true}'
```

用 Python 也一样：

```python
import os
import noeta
client = noeta.connect("http://127.0.0.1:8642", token=os.environ["NOETA_SERVER_TOKEN"])
task = await client.create("main", "列出所有 TODO", config={"permission_mode": "default"})
async for rec in client.watch(task):
    print(rec.seq, type(rec.event).__name__)
```
