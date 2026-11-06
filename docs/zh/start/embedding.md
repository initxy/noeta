# 在 Python 里嵌入

`noeta` 包本身也是一个库。这一页讲怎么在你自己的程序里跑 agent：创建任务、跟踪事件、回答审批和
提问、加一个工具、连接已有的存储或服务端。这里用到的每个名字都列在 [Python 接口](../reference/python-api.md)。

嵌入需要的东西都不在 `noeta.cli` 里，`noeta.cli` 只是 `noeta` 命令本身。

<Diagram name="start-embedding" />

## 几个组成部分

- **存储**（`noeta.store(url)`）保存所有任务：事件日志、收件箱和任务表里的一行。单机用
  `sqlite:///path`，多机用 `postgres://…`（需要 `postgres` 组件）。
- **代理**（`noeta.default_agents(model)`）：`main` 和三个子代理 `Explore`、`Plan`、
  `general-purpose`，用的是和 `noeta` 命令相同的代理文件。
- **provider**（`noeta.provider(family, …)`）负责和模型通信：`anthropic`、`openai_compat` 或
  `openai_responses`。
- **`Runtime`** 就是 worker：从存储里认领任务，一步步执行，任务开始等待或结束时交还。
- **`Client`** 创建任务并和它交互：`create`、`send`、`answer`、`signal`、`cancel`、`get`、
  `tasks`、`events`、`watch`、`wait`。它自己不执行任何东西，只往存储里写，由 Runtime 去接活。
- **`noeta.LocalApp`** 把存储、在后台跑的 Runtime 和客户端装进一个对象。终端界面、`noeta acp`、
  `noeta -p` 用的都是它。

## 第一个程序

```python
import asyncio
import noeta
from noeta.core.events import TaskEnded

async def main() -> None:
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5", one_shot=True)}
    model = noeta.provider("anthropic", api_key="sk-ant-...")   # 走网关就加 base_url=...
    runtime = noeta.Runtime(store=store, agents=agents, provider_for=lambda _model: model)
    client = noeta.Client(store)

    task = await client.create("main", "这个仓库是做什么的？",
                               config={"cwd": ".", "permission_mode": "default"})
    await runtime.run_until_idle()          # 一直跑，直到没有可认领的任务

    for rec in await client.events(task):
        if isinstance(rec.event, TaskEnded):
            print(rec.event.status, rec.event.answer)

asyncio.run(main())
```

逐行说明：

- `one_shot=True` 改的是 `main` 代理的一个字段：第一轮结束任务就结束，最后的回复在
  `TaskEnded.answer` 里。不加它，任务就是一场对话：每轮结束后等下一条消息（`Waiting`，等的是
  `"input"`），你再 `send` 给它。
- `config` 是任务的配置。`cwd` 是工具干活的目录。`permission_mode` 取 `default`、`acceptEdits`、
  `plan`、`auto`（默认）、`dontAsk`、`bypassPermissions` 之一。`auto` 模式下，本来要问人的操作由
  一次分类模型调用来判断；`default` 模式下它们会等人回答（见下文）。其他可设的键：`model`、
  `effort`、`max_turns`、`output_schema`、`timezone`。
- `run_until_idle()` 不断认领、执行任务，直到没有可认领的。等审批的任务不可认领，所以它会返回；
  回答之后再调一次。
- `provider_for` 把模型 id 映射到 provider。只有一个 provider 时可以不管这个 id。

## 一个对象全包：`LocalApp`

`LocalApp(store, agents, provider)` 的代理是个列表，provider 可以是一个 provider，也可以是一个
`provider_for` 函数。启动之后（或者用 `async with`）它在后台执行任务，你只需要跟踪，不用自己驱动
Runtime：

```python
import asyncio
import os
from collections.abc import Sequence

import noeta
from noeta.core.events import TaskEnded, ToolStarted, Waiting
from noeta.host.pending import pending_tree

@noeta.function_tool
def word_count(text: str) -> str:
    """Count the words in a text."""
    return str(len(text.split()))

class Counting(noeta.Extension):
    name = "counting"

    def tools(self, view: noeta.TaskView) -> Sequence[noeta.Tool]:
        return [word_count]

async def main() -> None:
    model = noeta.provider("anthropic", api_key=os.environ["ANTHROPIC_API_KEY"])
    agents = noeta.default_agents("claude-sonnet-5-5", extensions=[Counting()], one_shot=True)
    async with noeta.LocalApp(noeta.store("sqlite:///tasks.db"), agents, model) as app:
        task = await app.client.create("main", "How many words are in 'a b c'?",
                                       config={"cwd": os.getcwd(), "permission_mode": "default"})
        async for rec in app.client.watch(task):          # 任务结束时自动停
            event = rec.event
            if isinstance(event, Waiting) and event.on == "approval":
                for item in await pending_tree(app.client, task):
                    print(f"allowing {item.tool} {item.input}")
                    await app.client.answer(item.task_id, item.request_id, "allow")
            elif isinstance(event, ToolStarted):
                print("running", event.call.name)
            elif isinstance(event, TaskEnded):
                print(event.status, event.answer)

asyncio.run(main())
```

拿一个按脚本回复的假模型跑这段程序，输出是：

```text
allowing word_count {'text': 'a b c'}
running word_count
done Three words.
```

- `function_tool` 把带类型标注的函数变成工具：函数名是工具名，docstring 是描述，参数标注生成输入
  的 JSON Schema。`read_only=True` 表示只读，可以和其他只读调用并行执行。
- `extensions=[…]` 给每个代理加一个 `Extension`。插件能提供工具、上下文、提醒、斜杠命令，还能在
  工具调用前把关；详见[写一个插件](../guides/plugins.md)和 [Extension 接口](../reference/extension.md)。
- `watch(task, after=0)` 从 `after` 开始实时吐出每一条已记录的事件，任务结束就返回。每一项是一个
  `Recorded`（`seq`、`at`、`event`）；十种事件见[事件](../reference/events.md)。

### 实时输出

已记录的事件是一步一步来的。模型边生成边输出的文字要通过 `app.deltas(sink)` 拿：每来一个片段就调用
`sink(task_id, delta)`，返回值是取消订阅的函数：

```python
unsubscribe = app.deltas(
    lambda task_id, d: print(d.text, end="", flush=True) if d.kind == "text" else None)
```

`delta.kind` 是 `text`、`thinking` 或 `tool`（正在跑的工具最新的输出）。这些片段从不入库，以记录
下来的 `ModelReplied` 为准。用 Postgres 存储时，`LocalApp` 也能收到其他机器上的 worker 发出的片段。

## 多轮对话

不加 `one_shot` 的任务就是一场对话。一轮结束后再发下一条：

```python
task = await app.client.create("main", "读一下 README。", config={"cwd": "."})
row = await app.client.wait(task)            # 在等（等输入或等审批）或已结束
await app.client.send(task, "用一句话总结。")
```

- `send(task, text, when="next_turn")` 把消息排在当前这一轮之后；`when="now"` 让它在正在进行的
  这一轮的下一步就送到。
- `cancel(task)` 结束任务；`cancel(task, turn_only=True)` 只停下当前这一轮。
- `create(…, dedupe_key=…)` 和 `send(…, dedupe_key=…)` 是幂等的：重复调用拿到的是第一次的结果。
  想幂等地开启一场对话，就不带 `input` 创建任务（并指定自己的 `task_id`），然后每条消息用各自的
  key 去 `send`。
- `create(…, history_from=别的任务, history_through=seq)` 从另一个任务的对话出发新建任务，用于
  分叉或回退。

消息可以带图片：传一个 `noeta.core.types.Message`，里面放 `Text` 和 `Image` 块，而不是字符串。

## 审批和提问

需要人参与的调用会让任务挂起，记一条 `Waiting(on="approval")` 或 `Waiting(on="question")`；子代理
的等待记在它自己的任务上。`pending_tree(client, task)`（`noeta/host/pending.py`）列出一个任务及其
子代理里所有待处理的请求，每个是一个 `Pending`，带 `task_id`、`request_id`、`kind`、`tool`、
`input`、`prompt`、`detail`、`agent`。

- **审批：** `client.answer(task_id, request_id, "allow" | "deny", always=False)`。`always=True`
  会同时记住卡片上给出的那条规则（`detail["always"]`）。`noeta.host.pending.choices(detail)` 给出
  卡片上的选项（同意、"以后不再问"的规则、切换模式、拒绝），用 `answer_choice` 按选项回答。
- **提问**（`AskUserQuestion`）：问题在 `detail["questions"]` 里，每个有 `id`、`question`，可能
  有 `options` 和 `multiSelect`。用 `noeta.host.questions.answer_question(client, task_id,
  request_id, data)` 回答，`data` 把每个问题 id 映射到 `{"selected": [选项]}` 或
  `{"other": "文字"}`（只有一个问题时直接给字符串也行）。它会先校验，再提交。

同一个请求 id 的回答是幂等的：对同一请求的第二次回答会被忽略。

## 信号

`client.signal(task, name, data)` 发一个带名字的输入给任务里的某个插件读，不会开启新的一轮：

```python
await app.client.signal(task, "permission_mode", {"mode": "acceptEdits"})
await app.client.signal(task, "config", {"model": "claude-opus-5-5", "effort": "high"})
```

`wake=False` 让正在等待的任务继续等，下次醒来再读这个信号。

## 自己跑 worker

`LocalApp` 会把多余的关键字参数传给它的 `Runtime`。长期运行的服务通常每个进程起一个 `Runtime`，
调用 `serve(stop)`：

```python
stop = asyncio.Event()
runtime = noeta.Runtime(store=store, agents=agents, provider_for=provider_for,
                        concurrency=16, lease_seconds=30.0, queues=("default",),
                        spill_root="/var/lib/my-service/tool-results")
await runtime.serve(stop)          # 直到 stop 被设置；正在跑的任务会交还
```

- `concurrency`：同时跑几个任务。`queues`：这个 worker 从哪些队列认领（`client.create(…, queue=…)`
  决定任务进哪个队列）。
- `lease_seconds`：一次认领不续约能维持多久。worker 挂了，大约这么久之后它的任务就能被别人认领。
- `env_for(state)`：任务的工具在哪个执行环境里跑；默认是本机、任务的 `cwd`。容器执行环境在
  `noeta.backends.execenvs.container`（[命令在哪里执行](../guides/sandbox.md)）。
- `spill_root`：太长、没法直接返回的工具输出存在哪。`Runtime` 默认是 `None`，即直接截断；
  `LocalApp` 会设一个临时目录。
- `snapshot_every`：隔多少个事件写一次重放快照（默认 100；0 表示不写）。

单独的 `Runtime` 不会清理存下来的旧工具输出。在 `LocalApp` 上 `await app.run_until(stop)`，会一直运行它的
worker、渠道和每天一次的清理，直到 `stop` 被设上；`noeta worker` 就是这么做的。

同一个存储可以同时由其他机器上的 `noeta worker` 进程服务；它们任何一个都能接着跑任何任务
（[多机运行](../how-it-works/many-machines.md)）。`LocalApp(…, own_tasks_only=True)` 只服务自己
创建或接手的任务树，前端们就是这样共用 `~/.noeta/tasks.db` 而不抢彼此的任务。它把任务建在
`front-end` 队列上，`noeta serve` 和 `noeta worker` 不取这个队列（除非用 `--queue` 指定），所以
这样的任务只在打开着它的前端里跑，除非有人通过服务端对它操作（见 [HTTP 接口](../reference/http-api.md#和任务交互)）。

## 连接已有的存储或服务端

`noeta.connect(url)` 给你一个连向别处任务的客户端：

```python
client = noeta.connect("sqlite:////home/me/.noeta/tasks.db")    # 这个存储上的 Client
client = noeta.connect("postgres://noeta:…@db.internal/noeta")    # 同上
client = noeta.connect("https://noeta.internal:8642", token=os.environ["NOETA_SERVER_TOKEN"])
```

存储地址给的是本进程里的 `Client`：直接读写存储，得有某个 worker 去执行任务。`http(s)://` 地址给的
是 `RemoteClient`，方法相同，走 `noeta serve` 的 HTTP 接口
（[HTTP / WebSocket 接口](../reference/http-api.md)）；任务由服务端的 worker 执行。

## 和 `noeta` 命令的区别

`noeta` 命令在 `noeta.cli.config.from_settings` 里根据 `~/.noeta`（设置文件、代理文件、插件文件、
`config.toml`）构建代理。用 `default_agents` 构建代理的程序只拿到你传进去的东西：

- 记忆插件、MCP 插件和重复检测插件（`repetition_guard`）默认关闭，要在 `plugins` 里写上设置才开：
  `default_agents(model, plugins={"mcp": {"servers": {...}}, "memory": {"home": …}})`。
- 不读任何设置文件：权限规则作为权限插件的参数传进去，
  `plugins={"permissions": {"allow": ["Bash(git *)"], "mode": "acceptEdits"}}`。
- `plugins={"title": False}` 关掉某个插件。标题插件每场对话会额外调一次模型；`auto` 模式的分类器
  也会自己调模型。
- 模型目录里没有的模型，在你登记它的上限之前按 128,000 / 16,384 token 跑：
  `noeta.core.models.register_models({...})`。

代理也可以来自代理文件：`noeta.read_agent_dir(path)` 和 `noeta.parse_agent_file(text)` 返回
`AgentFile`，传给 `default_agents(model, extra_files=…)`。
