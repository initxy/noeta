# noeta 是怎么工作的

这一部分讲 noeta 的设计：每个机制是什么、为什么这样做、出了故障会怎样。本页是总图，每个机制各有一页细讲。

## 整体结构

noeta 由两部分组成：一个很小的内核，负责按只追加的日志把任务跑起来；一组插件，模型能做的每件事都由插件提供。

<Diagram name="core-architecture" />

- **前端**——终端界面、`noeta -p`、走 ACP 的编辑器、网页版和桌面版、你自己写的 Python 代码——都不直接碰任务。它们只调用一套接口，即客户端接口（`noeta/core/client.py`）：`create`、`send`、`answer`、`signal`、`cancel`、`get`、`tasks`、`events`、`watch`、`wait`。同进程里就是存储上的 `noeta.Client`；跨网络时，同一套接口搬到 HTTP 上，`watch` 用服务端推送的事件流（SSE）（`noeta serve`、`noeta.RemoteClient`）。ACP 和 Claude Code 的无界面协议（`noeta -p`、`stream-json`）都是把这套接口翻译了一遍，不是另起一套接口（见[网页、桌面与编辑器](../guides/front-ends.md)和 [HTTP 接口](../reference/http-api.md)）。
- **存储**保存所有任务：每个任务的事件日志、收件箱、`tasks` 表里的一行（日志的投影，和日志在同一个事务里写），以及折叠快照。自带两种实现：sqlite（单机，多个进程可以共用一个文件）和 Postgres（多机）（`noeta/backends/stores/`）。
- **工人**（`noeta worker`，或者 `noeta serve`、终端界面里内置的那个）从存储里认领一个可以跑的任务，折叠它的日志，调用模型，经由插件执行工具，再把发生的事追加进日志。任何一个工人都能接着跑任何一个任务，因为任务需要的一切都在日志里。
- **模型 provider** 通过适配器调用（`noeta/backends/providers/`：Anthropic、兼容 OpenAI 的 chat 接口、OpenAI Responses）。见 [provider 与模型路由](./models.md)。
- **执行环境**（`ExecEnv`，`noeta/core/execenv.py`）是任务的工具读文件、跑命令的地方：本机、沙箱或容器。见[命令在哪里执行](../guides/sandbox.md)。

箭头只有一个方向。前端要给任务说话，就往它的收件箱投递；要看任务，就读它的日志。前端从不追加事件，只有持有任务认领的那个工人才追加。

## 任务就是一条日志，以及其他几条原则

下面几条决定了其余的一切，每条都有专页。

| 原则 | 一句话 | 详见 |
|---|---|---|
| 任务就是一条日志 | 每个任务是一串只追加的事件，共十种；任务状态由日志折叠而来，下一次模型请求是这个状态的纯函数。 | [任务就是一条日志](./event-log.md) |
| 一次走一步 | 工人取收件箱、按折叠出的状态做决定，然后要么调用模型，要么让回复里的工具调用逐个过插件的关卡并执行，每个结果都追加进日志。 | [任务的一步](./one-step.md) |
| 收件箱是唯一的入口 | 消息、答复、取消、子任务结束、定时器、插件信号，一律投递进收件箱，可去重，只有持有它认领的工人能取。 | [等待与唤醒](./waiting.md) |
| 等待不花钱 | 需要人或子任务的调用会停下来等（park）：任务写一条 `Waiting`，交还认领，在有东西到达之前不占任何工人。 | [等待与唤醒](./waiting.md) |
| 只有一个写入者 | 每次追加都带上认领的 epoch 和期望的序号，所以卡住后丢了认领的工人再也写不进去。 | [单写者与崩溃恢复](./recovery.md) |
| 副作用绝不盲目重做 | 工具调用在执行前先记一笔“已开始”；崩溃之后，除非工具能查到结果或者声明可以安全重跑，否则一律答复“结果未知”。 | [单写者与崩溃恢复](./recovery.md) |
| 所有能力都是插件 | 文件工具、shell、权限、hooks、技能、子代理、MCP、记忆、压缩，全都是插件，接入同一个接口 `Extension`。 | [插件体系](./plugins.md) |
| 模型看到的东西对齐 Claude Code | 工具名和 schema、提示词的排布、提醒、压缩，都和 Claude Code 一致，并拿录下来的真实请求逐项比对。 | [如何对齐 Claude Code](./parity.md) |

建立在这些之上的部分：[上下文、缓存与压缩](./context.md)（请求怎么排布才能让 provider 的提示词缓存一直命中，长对话怎么做摘要）、[provider 与模型路由](./models.md)，以及[多机运行](./many-machines.md)（在一个 Postgres 存储上跑多台机器的工人）。

## 内核与插件

内核刻意做得很小。它会记日志、折叠日志、决定下一步、调用模型、让工具调用过关卡并执行、停下来等待、被唤醒、从崩溃中恢复。它不知道文件是什么，不知道权限规则写了什么，也不知道怎么压缩对话。这些都是插件的事。

<Diagram name="core-kernel-plugins" />

插件实现 `Extension`（`noeta/core/extension.py`）里的任意几个方法，每个都可选：提供工具、往系统提示词里加内容、在调用模型前加提醒、改写请求（`before_model`）、对每次工具调用表态（`before_tool`）、修改结果（`after_tool`）、观察每条事件（`on_event`）、提供斜杠命令、保存持久状态。

插件要记状态，从来不需要新的事件类型。插件写一条 `ExtensionRecorded(extension, kind, data)`，再由自己的 `reduce(state, kind, data)` 折回自己的状态。内核自己的流水账也用这同一种事件，记在 `extension="core"` 名下（`noeta/core/records.py`）。所以插件的状态和内核的一样持久、一样不挑机器：它就在日志里，任何工人都能重新折叠出来。

插件按名字通过 Python 的 entry point 找到；内置插件就在 `pyproject.toml` 里注册，和第三方包的方式完全一样；插件也可以只是一个文件。任务会记下自己用到了哪些插件。缺了其中某个插件的工人绝不会凑合着跑：它把任务留给装了这个插件的工人，只有连续三次认领都这样，才让任务失败（[插件版本记录](./recovery.md#plugin-pins)）。完整机制见[插件体系](./plugins.md)，动手写一个见[写一个插件](../guides/plugins.md)。

## 分层

代码按层组织，只准往下引用。`make check` 里的 `lint-imports`（规则写在 `pyproject.toml`）和 `scripts/lint-layout.py` 负责检查。

<Diagram name="core-layers" />

| 包 | 放什么 |
|---|---|
| `noeta/core` | 内核：事件、折叠（`loop/`）、运行器（`runner/`）、存储/客户端/插件/工具这几个接口、模型目录、重试。 |
| `noeta/toolkit` | 几个插件（或后端）共用的代码：路径、shell 解析、输出上限、沙箱、读取别的插件记录的类型化读取器。 |
| `noeta/plugins/<name>` | 各项能力。插件只引用 `core` 和 `toolkit`，从不引用别的插件。 |
| `noeta/backends` | 可替换的基础设施：模型 provider、执行环境（本机、沙箱、容器）、存储（sqlite、Postgres）。后端之间互不引用。 |
| `noeta/host` | 把 noeta 跑起来（`LocalApp`）以及各种接入方式：HTTP、ACP、stream-json。从不引用插件。 |
| `noeta/agents` | 代理文件格式和内置代理（`noeta/agents/files/*.md`）。 |
| `noeta/cli` | `noeta` 命令和它在 `~/.noeta` 里的配置。 |

除了 `noeta/cli`，其余都算 SDK。前端是独立的项目：`tui/`（终端界面，`noeta-tui`）跑在 `AppAPI` 上，可以同进程，也可以用 `--server` 连到服务器；`web/` 和 `desktop/` 走 HTTP。

还有三条规则让内核保持干净：

- `noeta/core/loop/`（折叠、组装请求、做决定）不引用运行器、存储和 provider：它是纯的，同一条日志永远折叠出同一个状态。
- 两个插件之间从不互相引用。一个插件要用另一个插件的东西，只能经过内核：按名字找工具（`TaskView.find_tool`）、通过 `noeta/toolkit/plugin_records.py` 里的类型化读取器读对方公开的记录、或者拿可运行的代理列表（`TaskView.agents`）。
- 前端和宿主不引用插件。它们要显示的东西（权限模式、标题、检查点）都按名字从插件的记录里读。

## 接下来读什么

- [任务就是一条日志](./event-log.md)：事件类型、折叠、快照。
- [任务的一步](./one-step.md)：运行器的循环、模型调用、工具关卡、并行调用。
- [等待与唤醒](./waiting.md)：收件箱、停下来等待、提问、定时器、保留认领、取消。
- [单写者与崩溃恢复](./recovery.md)：认领、epoch，模型调用或工具调用做到一半时会怎样。
- [上下文、缓存与压缩](./context.md)
- [插件体系](./plugins.md)
- [provider 与模型路由](./models.md)
- [多机运行](./many-machines.md)
- [如何对齐 Claude Code](./parity.md)
- [事件参考](../reference/events.md)：每种事件和它的字段。

这些设计背后的决定都以 ADR 的形式记在仓库里（[`docs/adr/`](https://github.com/initxy/noeta/blob/main/docs/adr/index.md)），术语表在 [`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md)。
