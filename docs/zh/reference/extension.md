# Extension 接口

`Extension` 的每个钩子，`TaskView`、`Tool`、`ToolContext`、`ToolOutput`、`ToolFlags` 的每个成员，以及它们用到的几个小类型，都照着 `noeta/core/extension.py`、`noeta/core/tool.py`、`noeta/core/records.py`、`noeta/core/questions.py` 整理。每个钩子什么时候调用、为什么这样设计，见[插件体系](../how-it-works/plugins.md)；完整的例子见[写一个插件](../guides/plugins.md)。

插件常用的名字都能从包的顶层导入：

```python
from noeta import (Extension, TaskView, Tool, ToolContext, ToolOutput, ToolChanges, ToolUse,
                   ContextPart, Reminder, Command, CommandContext, Allow, Deny, Ask, Rewrite,
                   StatusItem, Widget, Notice, UiItem,
                   Recorded, TaskState, ask, read_answer, text_output, function_tool)
```

## Extension

`class Extension`（`noeta/core/extension.py`）。所有方法都可选，默认什么都不做。

### 类属性

| 属性 | 类型 | 含义 |
|---|---|---|
| `name` | `str` | 插件的注册名，也是它的记录所用的名字。必填；同一个代理里不能重复。 |
| `add_to` | `ClassVar[tuple[str, ...]]` | 只对插件*文件*有效：它自己加入哪些代理（`"*"` 表示所有代理），排在代理自己的插件后面。已安装的包写了也不生效。宿主设置里写 `enabled = false` 仍然能关掉它。 |
| `folds` | `ClassVar[tuple[str, ...]]` | 除了自己的记录，`reduce` 还要折叠哪些名字下的记录，折进本插件的状态。 |

### 状态

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `initial_state(self)` | `Any` | 每次折叠一开始：还没有任何记录时的状态。默认 `None`。 |
| `reduce(self, state, kind: str, data)` | `Any` | 每次折叠时，按日志顺序，对本插件的每条记录（以及 `folds` 里那些名字的记录）调用一次。用户的消息被删掉时，也会带着 `records.ERASED_KIND`（`"core.erased"`）和被删的 seq 调用。必须是纯函数。 |
| `continue_state(self, state)` | `Any` | 另一个任务接着这个任务的对话跑时（分叉、恢复的子代理）：新任务从什么状态开始。默认全部带过去。 |

### 工具和上下文

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `tools(self, view: TaskView)` | `Sequence[Tool]` | 每一步。同名时第一个提供的插件说了算；每个进程只对一次冲突记一次日志。 |
| `tool_changes(self, view: TaskView, offered: Sequence[Tool])` | `ToolChanges \| None` | 每一步，所有插件的 `tools` 之后。`offered` 是套上代理允许、禁止清单之后的工具，不含别的插件的改动；每个插件拿到的 `offered` 都一样。加进来的工具如果被代理的清单拒绝，整份答复作废。 |
| `context(self, view: TaskView)` | `Sequence[ContextPart]` | 每次调用模型：系统提示里的文字。 |
| `async reminders(self, view: TaskView)` | `Sequence[Reminder]` | 每次调用模型之前，排在所有插件的 `before_model` 前面（和 Claude Code 一样，先加备注、再决定要不要压缩）。运行器把它们记下，放进这次请求、接在已记录的消息后面，所以 `before_model` 看得到它们。 |
| `inherit(self, state: TaskState)` | `Mapping[str, Any]` | 这个任务派出子任务时：在派出它的工具给的配置之上，再加上这些配置项。 |

### 调用模型

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `async on_start(self, view: TaskView)` | `None` | 运行器每次接手任务、第一步之前。 |
| `async before_model(self, view: TaskView, request: ModelRequest)` | `ModelRequest` | 每次调用模型之前、提醒之后，按插件顺序，每个拿到上一个返回的请求。可以抛 `TurnDone`（不调用，这一轮结束）、`RequestStale`（按新状态重新拼请求）或 `TurnFailed(reason, detail)`。 |
| `async on_context_overflow(self, view: TaskView, request: ModelRequest)` | `bool` | 模型以太长为由拒了 `request`。腾出地方（记下一次压缩）后返回 `True`，然后重新调用。谁都没腾，这一轮以 `context_overflow` 失败。写成 `(self, view)` 的老版本也照样会被调用。 |
| `async on_empty_reply(self, view: TaskView, response: ModelResponse)` | `Message \| None` | 回复以 `end_turn` 结束，却没有文字也没有工具调用。返回一条让它接着干的消息，第一个返回的算数。内核把它记进一条 `continue` 记录（原因 `empty_reply`），再调用一次模型。谁都没给，这一轮就不带回答地结束（`turn_done`，原因 `empty_reply`）。 |

### 工具调用

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `async before_tool(self, view: TaskView, call: ToolUse)` | `Gate \| None` | 每个调用，问所有插件。有 `Deny` 就拒；其次是第一个 `Rewrite`（改写后的调用再过一遍，最多改写三次）；再其次是第一个 `Ask`。`None` 表示没意见。 |
| `async resolve_ask(self, view: TaskView, call: ToolUse, asks: Sequence[Ask])` | `Gate \| None` | 调用马上要因为 `asks` 挂起，且没人拒绝。返回 `Allow` 或 `Deny` 就是替用户回答，`Rewrite` 用别的输入跑（再过一遍检查），`Ask` 换掉原来的询问；第一个答复算数。重启后同一个调用可能再过一遍检查，所以花了代价得到的答复应该记下来复用。 |
| `async after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput)` | `ToolOutput` | 调用跑完、结果记下之前，按插件顺序。 |

### 事件

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `async on_event(self, view: TaskView, recorded: Recorded)` | `None` | 每追加一个事件之后，带着追加那一刻的状态。这里记下的东西紧接着追加；对结束任务的那个 `TaskEnded`，这里记的会被丢掉。每个事件只送一次，崩溃会丢，除非 `delivered_through` 另有说明。 |
| `delivered_through(self, view: TaskView)` | `int \| None` | 本插件已经处理到日志的哪个 seq，从它自己的状态里读。任务每次被认领时，这个 seq 之后的事件会重新交给 `on_event`：做到至少送达一次。`None`（默认）：不重送。 |

### 压缩

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `async before_compact(self, view: TaskView, trigger: Literal["manual", "auto"], instructions: str)` | `str \| Deny \| None` | 压缩插件在写摘要之前来问。返回文字就加进摘要的要求里；返回 `Deny` 就跳过这次压缩。 |
| `async resident(self, view: TaskView)` | `Sequence[Message]` | 压缩时：放在保留消息后面、重新挂上的内容（某个清单、环境信息），记进 `Compacted.after`。 |
| `async lead_resident(self, view: TaskView)` | `Sequence[Message]` | 压缩时：放在摘要前面、重新生成的内容（项目说明、记忆索引），记进 `Compacted.lead`。 |

### 命令

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `async commands(self, ctx: CommandContext)` | `Sequence[Command]` | 前端列出斜杠命令时。 |
| `async run_command(self, ctx: CommandContext, name: str, args: str)` | `str` | 执行本插件某个 `template` 为空的命令 `/name args`：返回要发出的消息文字。 |

### 前端

| 方法 | 返回 | 什么时候调用 |
|---|---|---|
| `async ui(self, view: TaskView)` | `Sequence[UiItem]` | 前端来问任务此刻要显示什么时（`GET /tasks/{id}/ui`、`AppAPI.ui`），跟踪的日志每往前走一次就再问一次。和 `reminders` 一样从折叠后的状态算出来，要便宜，不能写记录。 |
| `async ui_call(self, ctx: CommandContext, name: str, args)` | JSON | 插件自己的前端代码来调（`POST /plugins/{name}/call`、`AppAPI.plugin_call`），`ctx.task_id` 一定有值。要长期留下的改动写进任务日志（发信号或写记录），不能只存在插件自己的内存里。不认识的 `name` 抛 `LookupError`（默认就是这样）。 |

任务配置的 `plugins_off`（通过 `config` 信号改）里点了名的插件，照样折叠自己的记录（`initial_state`、
`reduce`、`continue_state` 照常调用，它的状态、任务的快照和折叠版本都不变），但从下一轮起，这个任务不
再调用它的其他任何方法，不管是哪个 worker 在跑；子任务创建时带上父任务的 `plugins_off`。

`ToolSet(tools, name="tools")` 把一组普通的工具包成一个 `Extension`。

## 放行结论（Gate）

`before_tool` 和 `resolve_ask` 的答复；`Gate = Allow | Deny | Ask | Rewrite`。

| 类型 | 字段 | 含义 |
|---|---|---|
| `Allow` | `approve: bool = False`、`outright: bool = False` | 没意见。`approve`：插件替这个调用作保（比如允许它的 `PreToolUse` 钩子），能满足其他插件不是 `firm` 的询问。`outright`：权限层按自己的设置直接放行；工具会看到 `ToolContext.allowed_outright`。 |
| `Deny` | `reason: str = ""` | 拒绝；模型看到这个原因。 |
| `Ask` | `prompt: str = ""`、`detail: dict = {}`、`firm: bool = False` | 挂起调用，等人回答。`prompt` 是前端显示的文字。`firm` 的询问（询问规则、钩子发起的询问）不会被别的插件的 `Allow(approve=True)` 满足。 |
| `Rewrite` | `input: dict`、`name: str \| None = None` | 换个输入跑，或者换成另一个工具。 |

## 钩子返回的几种类型

| 类型 | 字段 | 含义 |
|---|---|---|
| `ContextPart` | `text: str`、`order: int = 100`、`before: str = ""` | 系统提示里的文字，按 `order` 换行接在后面，放在提示词那个带缓存的块里；带 `before` 时，插在提示词里第一行与它相同的那一行前面。必须是稳定的文字，会变的内容改成记一条消息。 |
| `Reminder` | `text: str`、`kind: str = "reminder"`、`data = None`、`origin: "system" \| "memory" = "system"`、`role: "user" \| "system" = "user"`、`order: int = 100` | 作为 `(extension, kind, data)` 连同消息一起记下。`role="user"` 的放进用户那一轮；`role="system"` 的是后面的一个系统轮。先放 user 的，再按 `order` 放 system 的。 |
| `Command` | `name`、`description`、`template = ""`、`argument_hint = ""`、`local = False` | 一个斜杠命令。`template` 里的 `{args}` 换掉后就是发出的消息；为空时由 `run_command` 给出。`local` 命令的文字只显示给用户，不会到模型那里。 |
| `CommandContext` | `cwd: str`、`task_id: str \| None = None` | 命令在哪里被列出或执行。 |
| `StatusItem` | `key: str`、`text: str`、`tone: str = "normal"` | 前端状态栏里的一小行；`tone` 取 `normal`、`ok`、`warn`、`error`。 |
| `Widget` | `key: str`、`markdown: str`、`placement: str = "above"` | 一块 markdown，放在输入框上面（`above`）或下面（`below`）。 |
| `Notice` | `key: str`、`text: str`、`level: str = "info"` | 给人看的提示，同一个 `key` 只显示一次；`level` 取 `info`、`warn`、`error`。 |
| `ToolChanges` | `add: Sequence[Tool] = ()`、`withhold: Mapping[str, str] = {}`、`entries: Sequence[ToolEntry] = ()` | 插件自己的工具之外再提供的工具；从请求里扣下的工具，各带一个调用时返回的错误（在任何检查之前就答复）；不是工具的请求条目。 |
| `ToolEntry` | `spec: ToolSpec`、`before: str = ""` | 工具清单里一个永远不会被调用的条目（厂商的模式开关），放在名为 `before` 的工具前面，否则放在不是 `listed_last` 的工具后面。 |

## TaskView

钩子看到的东西（`@dataclass class TaskView`）：只读的折叠状态，加上记录的办法。钩子里记的东西，在钩子返回后由运行器追加。

### 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `state` | `TaskState` | 折叠出来的任务状态（只读）。 |
| `complete` | `async (ModelRequest) -> ModelResponse`，或 `None` | 通过任务的 provider 调用模型，带重试，不算对话里的一步。 |
| `env` | `ExecEnv \| None` | 任务的执行环境。 |
| `post` | `async (body, dedupe_key=None)`，或 `None` | 往这个任务自己的收件箱投递。 |
| `count_tokens` | `async (text) -> int \| None`，或 `None` | provider 对"一条只含 `text` 的用户消息"的精确 token 数。 |
| `read_log` | `async (task_id, after=0) -> Sequence[Recorded]`，或 `None` | 另一个任务的日志，只读；不存在的任务读出来是空的。 |
| `child_tasks` | `async (task_id) -> list[str]`，或 `None` | 某个任务的子任务编号。 |
| `extensions` | `Sequence[Extension]` | 任务的插件，按代理里的顺序；这个任务关掉的（`plugins_off`）不在里面。 |
| `model` | `str` | 任务用的模型。 |
| `agents` | `Mapping[str, Agent]` | 这台工人能跑的所有代理。 |
| `allowed_tools` | `Sequence[str] \| None` | 代理的工具白名单（`None`：所有工具）。 |
| `denied_tools` | `Sequence[str]` | 代理从不提供的工具名。 |
| `keeps_tail` | `bool \| None` | 压缩向各插件要常驻内容时：摘要后面是否还保留消息。 |
| `kept` | `Sequence[Message]` | 压缩向各插件要常驻内容时：保留下来的那些消息。 |
| `spill_dir` | `str \| None` | 超长输出的存放目录（`None`：不存）。 |
| `identity` | `str` | 任务请求开头的身份行。 |
| `effort` | `Effort \| None` | 任务的推理强度。 |
| `next_request` | `() -> ModelRequest`，或 `None` | 下一次模型调用会以什么请求开始（不跑提醒和 `before_model`），给两轮之间要共用提示词缓存的工作用。 |
| `pending`、`creates`、`enqueued` | 列表 | 这个钩子暂存的东西；由运行器追加、投递。 |

### 属性和方法

| 成员 | 返回 | 含义 |
|---|---|---|
| `task_id` | `str` | 任务编号。 |
| `cwd` | `str` | 配置里的 `cwd`，否则执行环境的，否则 `""`。 |
| `ext_state(name)` | `Any` | 某个插件折叠出的状态。 |
| `folded(reducers=None)` | `TaskState` | 把这个 view 上暂存的记录都折叠进去之后的状态，在副本上算。 |
| `prompt(name, default)` | `str` | 这个任务里某个有名字的提示词的文字。 |
| `record(extension, kind, data=None, *, message=None)` | `None` | 记一条；带 `message` 时，这条消息也进对话。写一个内核不认识的 `core` 记录会抛 `ValueError`。 |
| `enqueue(body, *, dedupe_key=None, task_id=None)` | `None` | 在和钩子记录同一次追加里，投递到本任务（或另一个存在的任务）的收件箱，崩溃时两者要么都在要么都不在。 |
| `spawn(agent, prompt, *, config=None, queue=None, key="")` | `str`（子任务编号） | 在工具调用之外暂存一个子任务；和其他子任务一样被跟踪（`ChildEnded` 会进收件箱）。`key` 相同，编号就相同。 |
| `find_tool(name)` | `Tool \| None` | 这个任务以 `name` 提供的工具：一个插件用另一个插件工具的办法。直接调它的 `run` 不会再过检查。 |
| `deferred_tool_names()` | `list[str]` | 这个任务提供的延迟加载工具，排好序。 |
| `compacted(event: Compacted)` | `None` | 记下一次压缩（压缩插件唯一的内核事件）。 |
| `async residents(*, lead=False, keeps_tail=None, kept=())` | `list[Message]` | 所有插件的 `resident`（或 `lead_resident`）消息；某个插件出错就写日志并跳过。 |
| `notify(text, *, kind=None, data=None)` | `None` | 告诉用户一件事；前端显示，模型看不到。 |
| `set_timer(name, at, text="")` | `None` | 在 `at`（epoch 秒）之后、任务闲着时叫醒它，`text` 作为下一条消息；同名的会替换。持久：哪台工人都能触发。 |
| `clear_timer(name)` | `None` | 撤回一个计时器。 |
| `erase_messages(seqs)` | `None` | 把这些 seq 上用户的消息（以及它们的 `inline` 展开和 `memory` 召回）从对话里拿掉。 |

`notify`、`set_timer`、`clear_timer`、`erase_messages` 来自 `records.CoreNotices`，在 `ToolContext` 上也有。

## Tool

`class Tool(Protocol)`（`noeta/core/tool.py`）。

| 成员 | 类型 | 含义 |
|---|---|---|
| `name` | `str` | 模型看到的名字。 |
| `description` | `str` | 给模型看的说明；提示词 `tool/<name>` 可以覆盖它。 |
| `input_schema` | `dict` | 输入的 JSON Schema；除非 `unchecked_input`，在任何检查之前先按它校验。 |
| `read_only` | `bool` | 只读，不持有共享状态：同一次回复里可以和别的只读调用一起跑。 |
| `clearable` | `bool` | 结果可以通过再调一次拿回来，所以旧结果可以从上下文里清掉。 |
| `async run(input, ctx: ToolContext)` | `ToolOutput` | 执行调用。抛出的异常会变成一个写着异常的错误结果。 |
| `flags`（可选） | `ToolFlags` | 见下；没有时是 `NO_FLAGS`（用 `tool_flags(tool)` 读）。 |
| `async resume(input, ctx, outcome)`（可选） | `ToolOutput` | 会挂起的工具必须有：问题的 `outcome` 是 `Answer`，子任务的是 `{child_id: ChildEnded}`，等待被撤回时是 `None`。 |
| `async recover(input, ctx)`（可选） | `ToolOutput \| None` | 崩溃留下的没跑完的调用：按 `ctx.idempotency_key` 查到的结果；查不出来返回 `None`。比 `retry_safe` 先问。 |
| `async rule_inputs(input, view)`（可选） | `Sequence[(input, allow_too)]` | 权限规则还要把这个调用当作哪些别的输入来判断。 |

`function_tool(fn)` 或 `@function_tool(name=None, description=None, read_only=False, clearable=False, deferred=False)` 把一个带类型标注的函数变成工具：参数说明来自标注（`str`、`int`、`float`、`bool`、`list[...]`、`dict`、`Literal`、`X | None`、`Annotated[T, "说明"]`），工具说明来自 docstring；类型是 `ToolContext`（或名叫 `ctx`）的参数会拿到上下文（`noeta/toolkit/function_tool.py`）。

## ToolFlags

`@dataclass(frozen=True) class ToolFlags`：

| 标志 | 默认 | 含义 |
|---|---|---|
| `deferred` | `False` | 只把名字告诉模型，发出时带 `defer_loading`；经 `ToolSearch` 加载。 |
| `search_hint` | `""` | `ToolSearch` 按关键词搜时也能匹配到的几个词；不发给模型。 |
| `loaded_description` | `None` | 工具带着 `defer_loading` 发出时改用的说明。 |
| `listed_last` | `False` | 排在其他工具后面（把一组会变的工具放在缓存前缀的末尾）。 |
| `run_last` | `False` | 在同一次回复的其他调用之后再跑。 |
| `past_agent_lists` | `False` | 不管代理的允许、禁止清单怎么写都提供。 |
| `retry_safe` | `False` | 再跑一次不会改变外面的任何东西：崩溃后没跑完的调用会重跑（最多开始 `MAX_ATTEMPTS` = 3 次）。 |
| `silent_reply_after` | `None` | `"call"` 或 `"success"`：自用户上一条消息以来调用过它（或成功调用过）之后，空回复就结束这一轮。 |
| `unchecked_input` | `False` | 输入不校验，原样交给工具。 |
| `safe_while_planning` | `False` | 不是只读，但不改文件、不碰任务之外的东西；计划模式下不用询问就能跑。 |
| `max_result_chars` | `50_000` | 文字结果超过这个长度（最多 `RESULT_CEILING_CHARS` = 50,000）就存成文件、给预览；`None` 表示工具自己控制结果长度、不受此限。 |

## ToolOutput

`class ToolOutput(Struct)`；`text_output(text, *, is_error=False, **meta)` 生成一个纯文字的。

| 字段 | 类型 | 含义 |
|---|---|---|
| `content` | `list[Text \| Image \| ToolReference]` | 模型拿到的内容。 |
| `is_error` | `bool` | 是不是错误结果。 |
| `meta` | `dict` | 和结果一起记下，给前端和插件用；从不发给模型。 |
| `display` | `Display \| None` | 前端可以怎么显示：`kind`（`markdown`、`code`、`diff`、`table`、`progress`、`kv`）、`data`、`fallback`。 |
| `park` | `Park \| None` | 调用需要等待时，用它代替结果。 |
| `end_turn` | `str \| None` | 这次回复的结果都落下后结束这一轮，用这段文字作为回答（子代理交回结果）。 |
| `block_list` | `bool` | 即使是纯文字，也按块列表发出。 |

`Park(on: "question" | "children", keys: list[str] = [], detail: dict = {})`。问问题用 `ask({"question": ..., "header"?, "options"?: [{"label", "description"?}], "multiSelect"?}, ...)` 生成，`resume` 里用 `read_answer(outcome, asked)` 读答案（问题被撤回时返回 `None`；`noeta/core/questions.py`）。

## ToolContext {#toolcontext}

`@dataclass class ToolContext`：正在跑的工具看到的东西。

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_id`、`call_id` | `str` | 任务和这次调用。 |
| `idempotency_key`（属性） | `str` | `<task id>:<call id>`：这次调用不管在哪台工人上跑多少次都一样。可以交给能识别重复请求的外部系统，或者在 `recover` 里按它查结果。 |
| `env` | `ExecEnv` | 任务的执行环境。 |
| `state` | `Mapping[str, Any]` | 每个插件折叠出的状态，按名字（只读）。 |
| `record` | `(extension, kind, data) -> None` | 记一条；在这次调用的 `ToolFinished` 之前追加。写一个内核不认识的 `core` 记录会抛 `ValueError`。 |
| `spill_dir` | `str \| None` | 太长、无法直接返回的完整输出存到哪。 |
| `spawn` | `(agent, prompt, *, config=None, background=False, queue=None, history_from=None, history_through=None) -> str`，或 `None` | 暂存一个子任务，和这次调用的记录一起原子地创建，所以重试的调用不会建出第二个子任务。`history_from` 表示接着那个任务的对话。 |
| `post` | `async (body, task_id=None, dedupe_key=None)`，或 `None` | 在一步之外往这个任务的收件箱投递，或者投给另一个任务（不存在的任务会抛 `UnknownTask`）。 |
| `root_id` | `str` | 根任务的编号。 |
| `keep` | `() -> KeptClaim`，或 `None` | 让这台工人一直占着这个任务的认领，等待期间也不放，直到释放：给只存在于本进程里的工作用（后台任务）。 |
| `tasks`、`task` | 异步函数，或 `None` | 存储里任务表的行；某一个任务的行。 |
| `read_log` | `async (task_id, after=0)`，或 `None` | 另一个任务的记录。 |
| `by_model` | `bool` | 是不是模型发起的调用（插件自己跑工具时为 `False`）。 |
| `approved` | `bool` | 用户在审批提示里允许了这次调用。 |
| `allowed_outright` | `bool` | 权限层按设置直接放行，没有问任何人。 |
| `model`、`identity`、`effort` | | 和 `TaskView` 上的一样。 |
| `prompt` | `(name, default) -> str` | 这个任务里某个有名字的提示词的文字。 |
| `count_tokens` | `async (text) -> int \| None`，或 `None` | provider 给的精确 token 数。 |
| `complete` | `async (ModelRequest) -> ModelResponse`，或 `None` | 调用模型，带重试，不算一步。 |
| `progress` | `(text) -> None`，或 `None` | 让实时观看的前端看到调用正在做什么；从不记录，也不发给模型。 |

`KeptClaim`（`keep()` 的返回值）：`lost: bool`、`lease_seconds: float`、`on_lost(callback)`（认领丢了时调用一次；返回一个能撤回这个回调的函数）、`release()`。

## 钩子可以抛的异常

| 异常 | 从哪里抛 | 效果 |
|---|---|---|
| `TurnDone` | `before_model`、`reminders` | 这一轮不调用模型就算完成（某个本地命令已经做完了事）。 |
| `RequestStale` | `before_model` | 对话变了（压缩）：重新拼请求，每个钩子都看到新状态。 |
| `TurnFailed(reason, detail="")` | `before_model`、`reminders`、`on_context_overflow` | 这一轮以 `reason` 失败：交互式任务等下一条消息，一次性任务以失败结束。 |

其他异常怎么处理，见[插件体系](../how-it-works/plugins.md#when-a-hook-raises)。
