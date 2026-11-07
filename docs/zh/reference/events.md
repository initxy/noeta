# 事件

任务日志里可能出现的每种事件、每种收件箱条目，以及内核以 `extension="core"` 写下的每种记录，连同字段和写入时机。定义在 `noeta/core/events.py`、`noeta/core/records.py` 和 `noeta/core/types.py`。为什么日志要这样设计，见[任务就是一条日志](../how-it-works/event-log.md)。

## 存储和传输的格式

存下来的一条事件是一个 `Recorded`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `seq` | int | 在任务日志里的位置，从 1 开始。 |
| `at` | float | 存储提交它的时间（Unix 秒，存储的时钟）。 |
| `event` | `Event` | 下面十种之一。 |

事件是 msgspec 结构体，编码成 JSON，用 `type` 字段区分种类；取默认值的字段省略不写。`client.events(task_id)`、`client.watch(task_id)` 和 HTTP 接口返回的样子是这样：

```json
{"seq": 11, "at": 1793871720.07, "event": {"type": "Waiting", "on": "input",
  "detail": {"answer": "It says hello world."}}}
```

## 事件类型

### `TaskCreated`

总是 seq 1，任务创建时由存储写入（`client.create`，或者父任务的工具、hook 创建子任务时）。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `agent` | str | — | 代理注册时的名字。 |
| `config` | dict | `{}` | 任务配置（`model`、`cwd`、`effort`、`max_turns`、`unattended` 等）。 |
| `parent_id` | str \| null | null | 父任务，子代理才有。 |
| `root_id` | str \| null | null | 任务树的根。 |
| `history_from` | str \| null | null | 这个任务接续哪个任务的对话（分叉、回退、恢复子代理）。 |
| `history_through` | int \| null | null | 先折叠 `history_from` 的哪个序号之前的部分；没给的话，创建时钉在源任务当时的最后一个序号。 |

### `InputTaken`

每一步开始时，工人从收件箱取件时写入。

| 字段 | 类型 | 含义 |
|---|---|---|
| `items` | list[`InboxItem`] | 取走的条目，按收件箱顺序，完整抄录（见[收件箱条目](#inbox-items)）。 |
| `through` | int | 取到的最后一个收件箱序号；在它之前的条目都恰好被消费一次。 |

### `ModelCalled`

在模型请求发出之前写入。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `model` | str | — | 请求指定的模型。 |
| `request_hash` | str | — | 编码后请求的 SHA-256 的前 32 个十六进制字符。 |
| `attempt` | int | 1 | 1 加上它之前连续多少条没有回复的 `ModelCalled`（调用时崩溃）。一次调用内部的重试记为 `core/retry` 记录，不产生新的 `ModelCalled`。 |

### `ModelReplied`

模型回复完整之后写入。

| 字段 | 类型 | 含义 |
|---|---|---|
| `response` | `ModelResponse` | `message`（角色 `assistant`，内容块：`text`、`thinking`、`tool_use`）、`stop_reason`（`end_turn`、`tool_use`、`max_tokens`、`refusal`）、`usage`（`uncached`、`cache_read`、`cache_write`、`output`、`reasoning`）、`model`（provider 报告的模型名）、`cache_ttl`（这次写入的提示词缓存能活多久，比默认长时才有）。 |

空回复，以及在输出上限处被截断、里面什么都没有的回复，不会记下；它们的用量记进一条 `core/turn_done` 或 `core/continue`。

### `ToolStarted`

工具调用执行之前写入。一起执行的几个调用在一次追加里写入。

| 字段 | 类型 | 含义 |
|---|---|---|
| `call` | `ToolUse` | `id`、`name`、`input`：实际执行的调用。被插件改写过时和回复里的调用不同。开始过两次的调用（崩溃后重跑的 `retry_safe` 调用）会有两条。 |

### `ToolFinished`

调用有了结果时写入：执行完了、被拒绝了、被恢复了，或者在这一轮被叫停、失败时被答复了。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `result` | `ToolResult` | — | `call_id`、`content`（`text`、`image`、`tool_reference` 块）、`is_error`、`block_list`。这是模型看到的部分。 |
| `meta` | dict | `{}` | 给前端的数据（diff、退出码、`display`），从不发给模型。 |
| `end_turn` | str \| null | null | 这次回复的结果都落地后结束这一轮，以此为答案（子代理交回报告；人拒绝某个调用后为 `""`）。 |
| `note` | str \| null | null | 跟在这次回复所有结果后面的文字：人拒绝某个调用时的标记，或者人在同意时补充的话。 |

### `Compacted`

压缩插件对对话做摘要时写入。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `summary` | str | — | 摘要正文。 |
| `through` | int | — | 组装请求时，这个序号（含）之前的事件换成摘要。 |
| `kept` | list[`Message`] | `[]` | 留在摘要后面的消息。 |
| `usage` | `Usage` | 全 0 | 做摘要的调用花了多少。 |
| `lead` | list[`Message`] | `[]` | 放在摘要前面的消息。 |
| `after` | list[`Message`] | `[]` | 重新挂在保留消息之后的内容（在它们末尾的系统消息之前）。 |

对话变成：`lead`、摘要、`kept`、`through` 之后的消息、`after`。见[上下文、缓存与压缩](../how-it-works/context.md)。

### `Waiting`

任务停下来等待时写入。同一个事务里交还认领（除非保留认领），任务行变成 `waiting`。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `on` | str | — | `input`、`approval`、`question` 或 `children`。（`timer` 和 `external` 也是合法取值；工人自己等定时器时写的是 `input` 加 `until`。） |
| `ids` | list[str] | `[]` | `approval` 和 `question`：请求 id（即调用 id）。`children`：子任务 id。 |
| `until` | float \| null | null | 任务到这个时间自己变成可认领：最早的定时器。 |
| `detail` | dict | `{}` | 给前端显示的内容。`input`：`{"answer": ...}`，或 `{"turn_failed": "<原因>: <细节>"}`。`approval`：按调用 id，`{"prompt", ...}`。`question` / `children`：按调用 id，等待中调用的 detail 和 `on`。 |

### `TaskEnded`

只写一次，而且是最后一条。同一个事务里删掉任务的快照，给还活着的子任务发 `Cancel`，给父任务发 `ChildEnded`。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `status` | str | — | `done`、`failed` 或 `cancelled`。 |
| `answer` | str | `""` | 最终答案。 |
| `error` | str | `""` | 失败原因，以一个原因词开头，比如 `llm_error`、`context_overflow`、`max_turns`、`internal_error`、`needs_human`、`plugin_missing`、`unreadable_log`。 |

### `ExtensionRecorded`

插件自己的持久记录，只由这个插件的 `reduce`（以及在 `Extension.folds` 里声明了它的插件）折叠。内核自己的记录写在 `extension="core"` 名下（见下文）。

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `extension` | str | — | 插件名（`Extension.name`），或 `core`。 |
| `kind` | str | — | 插件自己定的记录种类。 |
| `data` | any | null | 内容，任意 JSON。 |
| `message` | `Message` \| null | null | 带消息时，内核也把它加进对话（一条提醒、一条给模型的通知、召回的记忆）。 |

每个内置插件记些什么，见[内置插件](./plugins.md)；哪些插件记录允许别的插件和前端读取，见 `noeta/toolkit/plugin_records.py`。

## 收件箱条目 {#inbox-items}

投递到任务的收件箱（`Store.post`），可用 `dedupe_key` 去重，由 `InputTaken` 抄进日志。一个 `InboxItem` 由 `seq`（收件箱自己的序号）、`at` 和 `body` 组成；`body` 用 `kind` 字段区分种类。

| `kind` | 结构体 | 字段 | 谁来投递 |
|---|---|---|---|
| `message` | `Say` | `message`（`Message`）、`when`（`now` 或 `next_turn`，默认 `next_turn`） | `client.send`、父任务、交回报告的子代理、插件。`now` 在下一步加入正在进行的一轮；`next_turn` 等这一轮结束。 |
| `cancel` | `Cancel` | `reason`（str）、`turn_only`（bool，默认 false） | `client.cancel`、结束中的父任务。`turn_only` 只叫停当前这一轮。 |
| `answer` | `Answer` | `request_id`（str）、`decision`（`allow` / `deny` / null）、`always`（bool）、`data`（any） | `client.answer`，去重键 `answer:<request_id>`。答复批准时，`data` 里的 `{"message": ...}` 是人补充的话；答复提问时，`data` 是 `{"answers": ...}`（`noeta/core/questions.py`）。 |
| `child_ended` | `ChildEnded` | `child_id`、`status`（`done` / `failed` / `cancelled`）、`answer`、`error`、`last_usage`（它最后一次回复的用量）、`tool_calls`、`output_tokens` | 子任务，和它自己的结束在同一个事务里，去重键 `child:<child_id>`。 |
| `timer` | `TimerFired` | `name` | 工人，为到点的定时器投递，去重键 `timer:<name>:<设定它的序号>`。 |
| `external` | `External` | `name`、`data` | `client.signal`、插件、外部渠道。名为 `config` 的信号从下一次模型调用起修改任务的 `model`、`effort`、`max_turns`、`output_schema` 等几个键（`noeta/core/loop/state.py` 里的 `CHANGEABLE_CONFIG`）。 |

## 内核记录

内核自己的条目：`ExtensionRecorded(extension="core", kind=..., data=...)`，每种 kind 有一个确定的格式（`noeta/core/records.py`）。插件不能写内核不认识的 `core` 记录。前端会读 `notice`、`retry`、`turn_failed`、`turn_stopped` 和 `spawned`；其余是内核的流水账。

| `kind` | `data` | 什么时候写 |
|---|---|---|
| `notice` | `text`，可选 `kind`、`data` | 插件或工具调用 `notify(...)`：给人看的，模型永远看不到。 |
| `retry` | `attempt`、`delay`、`error` | 一次失败的模型调用即将重试。 |
| `continue` | 可选 `reason`（`empty_reply`）、`model`、`usage` | 回复碰到输出上限，或者是空回复而某个插件选择继续；它的 `message` 请模型接着来。`usage` 是被丢弃那次回复的用量。 |
| `turn_stopped` | `partial`、`marker`、`thinking`（回复中途被叫停时才有） | 这一轮被叫停（`Cancel(turn_only=True)`）。已流出的文字保留，标记作为人下一轮的开头。 |
| `turn_failed` | `reason`、`detail` | 这一轮没法继续；交互式任务等下一条消息。 |
| `turn_done` | 可选 `reason`（`empty_reply`）、`model`、`usage` | 这一轮没有回复就结束了：某个 `before_model` 结束了它（`/compact`），或者模型什么都没回。 |
| `spent` | `model`、`usage`、`purpose` | 对话之外的模型调用（压缩的摘要调用、起标题）：花费计入任务的用量。 |
| `erased` | `seqs` | 这些序号上人发的消息，以及由它们展开出来的内容，从对话里移除（被 `UserPromptSubmit` hook 拦下）。每个有状态的插件 reducer 也会以 `core.erased` 被折叠一次。 |
| `parked` | `call_id`、`on`、`keys`、`detail` | 某个工具调用停下来等提问或等子任务。 |
| `spawned` | `children`：每个 `{"id", "call_id", "background"}` | 这个任务安排的子任务。 |
| `timer` | `name`、`at`、`text` | 设了一个定时器（`set_timer`）；同名的会被替换。 |
| `timer_cleared` | `name` | 清掉一个定时器。 |
| `prompts` | 名字 → 文本 | 任务第一次开始时读到的提示词覆盖文件。 |
| `plugins` | `pins`：每个 `{"name", "source", "version", "arguments"}` | 任务用到的插件；第一次开始时记，之后每次接手发现有变化时再记。 |
| `reclaimed` | — | 这次接手前，上一个认领没有交还（它的工人跑到一半死了）；计入[重启上限](../how-it-works/recovery.md#a-task-that-keeps-killing-its-worker)。 |

## 谁写什么

只有持有任务认领的工人追加事件；`TaskCreated` 由存储写。插件从不直接写事件：它在 hook 里记的东西（`TaskView.record`、`ToolContext.record`）由工人在 hook 返回后紧接着追加，和它所属的事件在同一次追加里。其他所有人，包括前端，都只投递收件箱条目。
