# Extension interface

Every hook of `Extension`, every member of `TaskView`, `Tool`, `ToolContext`, `ToolOutput` and
`ToolFlags`, and the small types they use, read from `noeta/core/extension.py`,
`noeta/core/tool.py`, `noeta/core/records.py` and `noeta/core/questions.py`. For when each hook
runs and why, see [the plugin system](../how-it-works/plugins.md); for a worked example, see
[Write a plugin](../guides/plugins.md).

The names a plugin usually needs import from the package root:

```python
from noeta import (Extension, TaskView, Tool, ToolContext, ToolOutput, ToolChanges, ToolUse,
                   ContextPart, Reminder, Command, CommandContext, Allow, Deny, Ask, Rewrite,
                   Recorded, TaskState, ask, read_answer, text_output, function_tool)
```

## Extension

`class Extension` (`noeta/core/extension.py`). Every method is optional; the defaults do nothing.

### Class attributes

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | The plugin's registry name and the name its records are written under. Required; unique within an agent. |
| `add_to` | `ClassVar[tuple[str, ...]]` | For a plugin *file*: the agents it joins by itself (`"*"` for every agent), after the agent's own plugins. Ignored for an installed package. `enabled = false` in the host's setting still turns it off. |
| `folds` | `ClassVar[tuple[str, ...]]` | Other record names whose records `reduce` also folds into this plugin's state. |

### State

| Method | Returns | Called |
|---|---|---|
| `initial_state(self)` | `Any` | Once per fold: the state before any record. Default `None`. |
| `reduce(self, state, kind: str, data)` | `Any` | For each of this plugin's records (and those of the names in `folds`), in log order, on every fold. Also with `records.ERASED_KIND` (`"core.erased"`) and the erased seqs when a person's messages are erased. Must be pure. |
| `continue_state(self, state)` | `Any` | When a task continues this one's conversation (a fork, a resumed sub-agent): the state it starts from. Default: all of it. |

### Tools and context

| Method | Returns | Called |
|---|---|---|
| `tools(self, view: TaskView)` | `Sequence[Tool]` | Every step. The first plugin offering a name wins; a clash is logged once per process. |
| `tool_changes(self, view: TaskView, offered: Sequence[Tool])` | `ToolChanges \| None` | Every step, after every plugin's `tools`. `offered` is their tools after the agent's allow and deny lists, never another plugin's changes; every plugin gets the same `offered`. An added tool the agent's lists refuse drops the whole answer. |
| `context(self, view: TaskView)` | `Sequence[ContextPart]` | Every model call: text for the system prompt. |
| `async reminders(self, view: TaskView)` | `Sequence[Reminder]` | Before every model call. The runner records them and puts them in this request after the recorded messages. |
| `inherit(self, state: TaskState)` | `Mapping[str, Any]` | When this task spawns a child: config entries laid over the config the spawning tool gave it. |

### The model call

| Method | Returns | Called |
|---|---|---|
| `async on_start(self, view: TaskView)` | `None` | Each time a runner picks the task up, before the first step. |
| `async before_model(self, view: TaskView, request: ModelRequest)` | `ModelRequest` | Before every model call, after the reminders, in plugin order; each gets the previous one's request. May raise `TurnDone` (no call; the turn ends), `RequestStale` (the request is built again from the new state) or `TurnFailed(reason, detail)`. |
| `async on_context_overflow(self, view: TaskView, request: ModelRequest)` | `bool` | The model refused `request` as too long. Return `True` after making room (recording a compaction); the call is made again. When none does, the turn fails with `context_overflow`. A hook written as `(self, view)` is still called. |
| `async on_empty_reply(self, view: TaskView, response: ModelResponse)` | `Message \| None` | The reply ended (`end_turn`) with no text and no tool call. A message to go on with; the first one wins. The core records it in a `continue` entry (reason `empty_reply`) and calls the model again. |

### Tool calls

| Method | Returns | Called |
|---|---|---|
| `async before_tool(self, view: TaskView, call: ToolUse)` | `Gate \| None` | For each call, every plugin. Any `Deny` wins, then the first `Rewrite` (the rewritten call is gated again, at most three rewrites), then the first `Ask`. `None` is no opinion. |
| `async resolve_ask(self, view: TaskView, call: ToolUse, asks: Sequence[Ask])` | `Gate \| None` | The call is about to park on `asks` and nobody denied it. `Allow` or `Deny` answers in the person's place, `Rewrite` runs it with other input (gated again), `Ask` replaces the asks; the first answer wins. A call can be gated again after a restart, so an answer that cost something should be recorded and reused. |
| `async after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput)` | `ToolOutput` | After the call ran, before its result is recorded, in plugin order. |

### Events

| Method | Returns | Called |
|---|---|---|
| `async on_event(self, view: TaskView, recorded: Recorded)` | `None` | After each appended event, with the state as of the append. Records made here are appended right after; for the `TaskEnded` that ends the task they are dropped. Each event once, lost on a crash, unless `delivered_through` says otherwise. |
| `delivered_through(self, view: TaskView)` | `int \| None` | The log seq through which this plugin has handled its events, read from its own state. Whenever the task is claimed, the events after it are passed to `on_event` again: at least once. `None` (default): no redelivery. |

### Compaction

| Method | Returns | Called |
|---|---|---|
| `async before_compact(self, view: TaskView, trigger: Literal["manual", "auto"], instructions: str)` | `str \| Deny \| None` | By the compaction plugin, before it summarizes. Text is added to the summary's instructions; `Deny` skips this compaction. |
| `async resident(self, view: TaskView)` | `Sequence[Message]` | While compacting: content to re-attach after the kept messages (a listing, the environment). Recorded in `Compacted.after`. |
| `async lead_resident(self, view: TaskView)` | `Sequence[Message]` | While compacting: content to rebuild ahead of the summary (instructions, the memory index). Recorded in `Compacted.lead`. |

### Commands

| Method | Returns | Called |
|---|---|---|
| `async commands(self, ctx: CommandContext)` | `Sequence[Command]` | When a front end lists slash commands. |
| `async run_command(self, ctx: CommandContext, name: str, args: str)` | `str` | `/name args` for one of this plugin's commands whose `template` is empty: the message text. |

`ToolSet(tools, name="tools")` wraps a plain list of tools as an `Extension`.

## Gates

The answers of `before_tool` and `resolve_ask`; `Gate = Allow | Deny | Ask | Rewrite`.

| Type | Fields | Meaning |
|---|---|---|
| `Allow` | `approve: bool = False`, `outright: bool = False` | No objection. `approve`: the plugin vouches for the call (a `PreToolUse` hook that allows it), satisfying other plugins' asks that are not `firm`. `outright`: a permission layer allows it by its own settings; the tool sees `ToolContext.allowed_outright`. |
| `Deny` | `reason: str = ""` | Refuse; the model sees the reason. |
| `Ask` | `prompt: str = ""`, `detail: dict = {}`, `firm: bool = False` | Park the call until someone answers. `prompt` is what a front end shows. A `firm` ask (an ask rule, a hook asking) is not satisfied by another plugin's approving `Allow`. |
| `Rewrite` | `input: dict`, `name: str \| None = None` | Run the call with other input, or as another tool. |

## Parts the hooks return

| Type | Fields | Meaning |
|---|---|---|
| `ContextPart` | `text: str`, `order: int = 100`, `before: str = ""` | System prompt text, appended after a newline in `order`, inside the prompt's cached block; with `before`, placed ahead of the prompt's first line equal to it. Must be stable: text that changes is recorded as a message instead. |
| `Reminder` | `text: str`, `kind: str = "reminder"`, `data = None`, `origin: "system" \| "memory" = "system"`, `role: "user" \| "system" = "user"`, `order: int = 100` | Recorded as `(extension, kind, data)` with its message. `role="user"` rides in the person's turn; `role="system"` is a system turn after it. User ones first, then system ones by `order`. |
| `Command` | `name`, `description`, `template = ""`, `argument_hint = ""`, `local = False` | A slash command. `template` with `{args}` is the message sent; empty means `run_command` gives it. A `local` command's text is shown to the person and never reaches the model. |
| `CommandContext` | `cwd: str`, `task_id: str \| None = None` | Where a command is listed or run. |
| `ToolChanges` | `add: Sequence[Tool] = ()`, `withhold: Mapping[str, str] = {}`, `entries: Sequence[ToolEntry] = ()` | Tools offered besides the plugins' own; offered tools left out of the request, each with the error a call to it gets (answered before any gate); request entries that are no tool. |
| `ToolEntry` | `spec: ToolSpec`, `before: str = ""` | A tool-list entry that is never called (a vendor's mode switch), placed right before the tool named `before`, else after the tools that are not `listed_last`. |

## TaskView

What a hook sees (`@dataclass class TaskView`): the folded state, read only, and ways to record.
Records made during a hook are appended by the runner right after the hook returns.

### Fields

| Field | Type | Meaning |
|---|---|---|
| `state` | `TaskState` | The folded task state (read only). |
| `complete` | `async (ModelRequest) -> ModelResponse`, or `None` | Call the model through the task's providers, retried, not recorded as a step. |
| `env` | `ExecEnv \| None` | The task's exec env. |
| `post` | `async (body, dedupe_key=None)`, or `None` | Post to this task's own inbox. |
| `count_tokens` | `async (text) -> int \| None`, or `None` | The provider's exact count of one user message holding `text`. |
| `read_log` | `async (task_id, after=0) -> Sequence[Recorded]`, or `None` | Another task's log, read only; an unknown task reads as empty. |
| `child_tasks` | `async (task_id) -> list[str]`, or `None` | The ids of a task's children. |
| `extensions` | `Sequence[Extension]` | The task's plugins, in the agent's order. |
| `model` | `str` | The model the task runs on. |
| `agents` | `Mapping[str, Agent]` | Every agent this worker can run. |
| `allowed_tools` | `Sequence[str] \| None` | The agent's tool allowlist (`None`: every tool). |
| `denied_tools` | `Sequence[str]` | Tool names the agent never offers. |
| `keeps_tail` | `bool \| None` | While a compaction asks for residents: whether messages stay after its summary. |
| `kept` | `Sequence[Message]` | While a compaction asks for residents: the messages that stay. |
| `spill_dir` | `str \| None` | The spill root for long outputs (`None`: nothing is saved). |
| `identity` | `str` | The identity line the task's requests open with. |
| `effort` | `Effort \| None` | The task's effort. |
| `next_request` | `() -> ModelRequest`, or `None` | The request the next model call would start from (no reminder or `before_model` run), for work between turns that shares the prompt cache. |
| `pending`, `creates`, `enqueued` | lists | What this hook staged; the runner appends and posts them. |

### Properties and methods

| Member | Returns | Meaning |
|---|---|---|
| `task_id` | `str` | The task's id. |
| `cwd` | `str` | The config's `cwd`, else the exec env's, else `""`. |
| `ext_state(name)` | `Any` | A plugin's folded state. |
| `folded(reducers=None)` | `TaskState` | The state with every record pending on this view folded in, on a copy. |
| `prompt(name, default)` | `str` | The task's text for a named prompt. |
| `record(extension, kind, data=None, *, message=None)` | `None` | Record an entry; with `message`, it also joins the conversation. A `core` record of an unknown kind raises `ValueError`. |
| `enqueue(body, *, dedupe_key=None, task_id=None)` | `None` | Post to this task's inbox (or another existing task's) in the same append as the hook's records, so a crash keeps both or neither. |
| `spawn(agent, prompt, *, config=None, queue=None, key="")` | `str` (child id) | Stage a child task outside a tool call; tracked like any child (`ChildEnded` arrives in the inbox). The same `key` gives the same id. |
| `find_tool(name)` | `Tool \| None` | The tool this task offers under `name`, how one plugin uses another's tool. Calling its `run` directly is not gated again. |
| `deferred_tool_names()` | `list[str]` | The deferred tools this task offers, sorted. |
| `compacted(event: Compacted)` | `None` | Record a compaction (the compaction plugin's one core event). |
| `async residents(*, lead=False, keeps_tail=None, kept=())` | `list[Message]` | Every plugin's `resident` (or `lead_resident`) messages; a failing plugin is logged and skipped. |
| `notify(text, *, kind=None, data=None)` | `None` | Tell the person; front ends show it, the model never sees it. |
| `set_timer(name, at, text="")` | `None` | Wake the task at `at` (epoch seconds) once idle, with `text` as the next message; same name replaces. Durable: any worker fires it. |
| `clear_timer(name)` | `None` | Take a timer back. |
| `erase_messages(seqs)` | `None` | Take the person's messages at those log seqs (and their `inline` expansions and `memory` recalls) out of the conversation. |

`notify`, `set_timer`, `clear_timer` and `erase_messages` come from `records.CoreNotices` and
exist on `ToolContext` too.

## Tool

`class Tool(Protocol)` (`noeta/core/tool.py`).

| Member | Type | Meaning |
|---|---|---|
| `name` | `str` | The model-visible name. |
| `description` | `str` | The model-facing description; the prompt `tool/<name>` overrides it. |
| `input_schema` | `dict` | JSON Schema of the input; checked before any gate unless `unchecked_input`. |
| `read_only` | `bool` | Only reads and holds no shared state: may run alongside other read-only calls of the same reply. |
| `clearable` | `bool` | Its result can be fetched again, so an old one may be cleared from the context. |
| `async run(input, ctx: ToolContext)` | `ToolOutput` | Run the call. An exception becomes an error result naming it. |
| `flags` (optional) | `ToolFlags` | See below; `NO_FLAGS` when absent (read with `tool_flags(tool)`). |
| `async resume(input, ctx, outcome)` (optional) | `ToolOutput` | Required of a tool that parks: `outcome` is the `Answer` for a question, `{child_id: ChildEnded}` for children, `None` when the wait was withdrawn. |
| `async recover(input, ctx)` (optional) | `ToolOutput \| None` | For a call a crash left unfinished: its result, looked up by `ctx.idempotency_key`; `None` when it cannot be told. Asked before `retry_safe`. |
| `async rule_inputs(input, view)` (optional) | `Sequence[(input, allow_too)]` | Other inputs the permission rules also judge the call as. |

`function_tool(fn)` or `@function_tool(name=None, description=None, read_only=False,
clearable=False, deferred=False)` makes a tool from a typed function: its schema from the
annotations (`str`, `int`, `float`, `bool`, `list[...]`, `dict`, `Literal`, `X | None`,
`Annotated[T, "text"]`), its description from the docstring; a parameter typed `ToolContext` (or
named `ctx`) gets the context (`noeta/toolkit/function_tool.py`).

## ToolFlags

`@dataclass(frozen=True) class ToolFlags`:

| Flag | Default | Meaning |
|---|---|---|
| `deferred` | `False` | Named to the model by name only and sent with `defer_loading`; loaded through `ToolSearch`. |
| `search_hint` | `""` | Words a `ToolSearch` keyword query also matches; never sent. |
| `loaded_description` | `None` | The description sent instead while the tool goes with `defer_loading`. |
| `listed_last` | `False` | Sent after the other tools (a changing set at the end of the cached prefix). |
| `run_last` | `False` | Runs after the reply's other calls. |
| `past_agent_lists` | `False` | Offered whatever the agent's allow and deny lists say. |
| `retry_safe` | `False` | Running it again changes nothing outside: an unfinished call runs again after a crash (up to `MAX_ATTEMPTS` = 3 starts). |
| `silent_reply_after` | `None` | `"call"` or `"success"`: an empty reply after such a call since the person's last message ends the turn. |
| `unchecked_input` | `False` | Input goes to the tool unchecked. |
| `safe_while_planning` | `False` | Not read-only, yet changes no file and nothing outside the task; plan mode lets it run without asking. |
| `max_result_chars` | `50_000` | A text result longer than this (at most `RESULT_CEILING_CHARS` = 50,000) is saved to a file with a preview; `None` exempts a tool that bounds its own result. |

## ToolOutput

`class ToolOutput(Struct)`; `text_output(text, *, is_error=False, **meta)` makes a text one.

| Field | Type | Meaning |
|---|---|---|
| `content` | `list[Text \| Image \| ToolReference]` | What the model gets. |
| `is_error` | `bool` | An error result. |
| `meta` | `dict` | Recorded with the result for front ends and plugins; never sent to the model. |
| `display` | `Display \| None` | How a front end may show it: `kind` (`markdown`, `code`, `diff`, `table`, `progress`, `kv`), `data`, `fallback`. |
| `park` | `Park \| None` | Set instead of a result when the call must wait. |
| `end_turn` | `str \| None` | End the turn once this reply's results land, with this text as the answer (a sub-agent's hand-back). |
| `block_list` | `bool` | Send the result as a list of blocks even when it is plain text. |

`Park(on: "question" | "children", keys: list[str] = [], detail: dict = {})`. A question is made
with `ask({"question": ..., "header"?, "options"?: [{"label", "description"?}], "multiSelect"?},
...)`, and its `resume` reads the answer with `read_answer(outcome, asked)` (`None` when the
question was withdrawn; `noeta/core/questions.py`).

## ToolContext {#toolcontext}

`@dataclass class ToolContext`: what a running tool sees.

| Field | Type | Meaning |
|---|---|---|
| `task_id`, `call_id` | `str` | The task and this call. |
| `idempotency_key` (property) | `str` | `<task id>:<call id>`: the same for every run of this call on any worker. Pass it to an outside system that can drop a repeat, or look the outcome up by it in `recover`. |
| `env` | `ExecEnv` | The task's exec env. |
| `state` | `Mapping[str, Any]` | Every plugin's folded state, by name (read only). |
| `record` | `(extension, kind, data) -> None` | Record an entry; appended just before the call's `ToolFinished`. |
| `spill_dir` | `str \| None` | Where full outputs too large to return go. |
| `spawn` | `(agent, prompt, *, config=None, background=False, queue=None, history_from=None, history_through=None) -> str`, or `None` | Stage a child task, created atomically with this call's record, so a retried call never makes a second child. `history_from` continues that task's conversation. |
| `post` | `async (body, task_id=None, dedupe_key=None)`, or `None` | Post to this task's inbox from outside a step, or to another task (an unknown one raises `UnknownTask`). |
| `root_id` | `str` | The root task's id. |
| `keep` | `() -> KeptClaim`, or `None` | Keep this task's claim with this worker, over its waits too, until released: for work that lives only in this process (a background job). |
| `tasks`, `task` | `async`, or `None` | Rows of the store's task table; one task's row. |
| `read_log` | `async (task_id, after=0)`, or `None` | Another task's records. |
| `by_model` | `bool` | The model made this call (`False` when a plugin runs a tool itself). |
| `approved` | `bool` | A person allowed this call at its prompt. |
| `allowed_outright` | `bool` | A permission layer allowed it by its settings, with nobody asked. |
| `model`, `identity`, `effort` | | As on `TaskView`. |
| `prompt` | `(name, default) -> str` | The task's text for a named prompt. |
| `count_tokens` | `async (text) -> int \| None`, or `None` | The provider's exact count. |
| `complete` | `async (ModelRequest) -> ModelResponse`, or `None` | Call the model, retried, not a step. |
| `progress` | `(text) -> None`, or `None` | Show front ends watching live what the call is doing; never recorded nor sent to the model. |

`KeptClaim` (what `keep()` returns): `lost: bool`, `lease_seconds: float`, `on_lost(callback)`
(called once the claim is lost; returns a function that takes it back), `release()`.

## Exceptions a hook may raise

| Exception | From | Effect |
|---|---|---|
| `TurnDone` | `before_model`, `reminders` | The turn is complete without a model call (a local command did its work). |
| `RequestStale` | `before_model` | The conversation changed (a compaction): the request is built again, every hook seeing the new state. |
| `TurnFailed(reason, detail="")` | `before_model`, `reminders`, `on_context_overflow` | The turn fails with `reason`: an interactive task waits for the next message, a one-shot task ends failed. |

Any other exception is handled as [the plugin system page](../how-it-works/plugins.md#when-a-hook-raises)
describes.
