# Events

Every event a task's log can hold, every inbox item, and every record the kernel writes under
`extension="core"`, with their fields and when each is written. The definitions are
`noeta/core/events.py`, `noeta/core/records.py` and `noeta/core/types.py`. Why the log is built
this way is on [A task is a log](../how-it-works/event-log.md).

## On the wire

A stored event is a `Recorded`:

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | Its position in the task's log, from 1. |
| `at` | float | When the store committed it (epoch seconds, the store's clock). |
| `event` | `Event` | One of the ten types below. |

Events are msgspec structs encoded as JSON, tagged by a `type` field; fields at their default
value are left out. What `client.events(task_id)`, `client.watch(task_id)` and the HTTP API return
looks like this:

```json
{"seq": 11, "at": 1793871720.07, "event": {"type": "Waiting", "on": "input",
  "detail": {"answer": "It says hello world."}}}
```

## Event types

### `TaskCreated`

Always seq 1, written by the store when the task is created (`client.create`, or a parent's
tool or hook spawning a child).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `agent` | str | — | The agent's registered name. |
| `config` | dict | `{}` | The task's config (`model`, `cwd`, `effort`, `max_turns`, `unattended`, …). |
| `parent_id` | str \| null | null | The parent task, for a sub-agent. |
| `root_id` | str \| null | null | The root of the task tree. |
| `history_from` | str \| null | null | A task whose conversation this one continues (fork, rewind, resumed sub-agent). |
| `history_through` | int \| null | null | The last event of `history_from` folded first; pinned to the source's last seq at creation when unset. |

### `InputTaken`

Written at a step boundary when the runner takes items from the inbox.

| Field | Type | Meaning |
|---|---|---|
| `items` | list[`InboxItem`] | The items taken, in inbox order, each whole (see [Inbox items](#inbox-items)). |
| `through` | int | The last inbox seq taken; everything up to it is consumed exactly once. |

### `ModelCalled`

Written just before a model request is sent.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | str | — | The model the request names. |
| `request_hash` | str | — | The first 32 hex digits of the SHA-256 of the encoded request. |
| `attempt` | int | 1 | 1 + the `ModelCalled` events in a row before it without a reply (a crash during the call). Retries inside one call are `core/retry` records, not new `ModelCalled`. |

### `ModelReplied`

Written when the model's reply is complete.

| Field | Type | Meaning |
|---|---|---|
| `response` | `ModelResponse` | `message` (role `assistant`, content blocks: `text`, `thinking`, `tool_use`), `stop_reason` (`end_turn`, `tool_use`, `max_tokens`, `refusal`), `usage` (`uncached`, `cache_read`, `cache_write`, `output`, `reasoning`), `model` (as the provider reported it), `cache_ttl` (how long the prompt cache written lives, when longer than the default). |

An empty reply, and a reply cut at the output limit with nothing in it, are not recorded; their
usage goes into a `core/turn_done` or `core/continue` record.

### `ToolStarted`

Written just before a tool call runs. Several calls that run together are written in one append.

| Field | Type | Meaning |
|---|---|---|
| `call` | `ToolUse` | `id`, `name`, `input`: the call as it runs. It differs from the reply's call when a plugin rewrote it. A call started twice (a `retry_safe` call re-run after a crash) has two. |

### `ToolFinished`

Written when a call has its result: it ran, was refused, was recovered, or was answered when its
turn stopped or failed.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `result` | `ToolResult` | — | `call_id`, `content` (`text`, `image`, `tool_reference` blocks), `is_error`, `block_list`. What the model sees. |
| `meta` | dict | `{}` | Data for front ends (a diff, an exit code, a `display`); never sent to the model. |
| `end_turn` | str \| null | null | The turn ends once the reply's results land, with this as its answer (a sub-agent's hand-back; `""` after a declined call). |
| `note` | str \| null | null | Text that follows the reply's results in their message: the marker of a call the person declined, or the words they added to a yes. |

### `Compacted`

Written by the compaction plugin when it summarizes the conversation.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `summary` | str | — | The summary text. |
| `through` | int | — | Events up to and including this seq are replaced by the summary when requests are built. |
| `kept` | list[`Message`] | `[]` | Messages kept after the summary. |
| `usage` | `Usage` | zeros | What the summarize calls cost. |
| `lead` | list[`Message`] | `[]` | Messages placed before the summary. |
| `after` | list[`Message`] | `[]` | Messages re-attached after the kept messages (ahead of the system turns they end with). |

The conversation becomes `lead`, the summary, `kept`, the messages after `through`, then
`after`. See [Context, caching and compaction](../how-it-works/context.md).

### `Waiting`

Written when the task stops to wait. In the same transaction the claim is given back (unless it
is kept) and the row becomes `waiting`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `on` | str | — | `input`, `approval`, `question` or `children`. (`timer` and `external` are also accepted values; the runner itself waits for timers as `input` with `until`.) |
| `ids` | list[str] | `[]` | For `approval` and `question`: the request ids (the call ids). For `children`: the child task ids. |
| `until` | float \| null | null | When the task becomes claimable on its own: the earliest timer. |
| `detail` | dict | `{}` | What front ends show. For `input`: `{"answer": ...}`, or `{"turn_failed": "<reason>: <detail>"}`. For `approval`: per call id, `{"prompt", ...}`. For `question` / `children`: per call id, the parked call's detail and `on`. |

### `TaskEnded`

Written once, last. In the same transaction the task's snapshots are deleted, its live children
are sent a `Cancel`, and its parent is sent `ChildEnded`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `status` | str | — | `done`, `failed` or `cancelled`. |
| `answer` | str | `""` | The final answer. |
| `error` | str | `""` | Why it failed: starts with a reason such as `llm_error`, `context_overflow`, `max_turns`, `internal_error`, `needs_human`, `plugin_missing`, `unreadable_log`. |

### `ExtensionRecorded`

A plugin's own durable entry, folded only by that plugin's `reduce` (and by plugins that declare
it in `Extension.folds`). The kernel writes its own under `extension="core"` (below).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `extension` | str | — | The plugin's name (`Extension.name`), or `core`. |
| `kind` | str | — | The plugin's own record kind. |
| `data` | any | null | Its payload, any JSON. |
| `message` | `Message` \| null | null | With a message, the kernel also adds it to the conversation (a reminder, a notice for the model, recalled memory). |

What each built-in plugin records is on [Built-in plugins](./plugins.md); which plugin records
other plugins and front ends may read is in `noeta/toolkit/plugin_records.py`.

## Inbox items

Posted to a task's inbox (`Store.post`), each idempotent on an optional `dedupe_key`, and copied
into the log by `InputTaken`. An `InboxItem` is `seq` (the inbox's own sequence), `at` and
`body`; the body is tagged by a `kind` field.

| `kind` | Struct | Fields | Posted by |
|---|---|---|---|
| `message` | `Say` | `message` (`Message`), `when` (`now` or `next_turn`, default `next_turn`) | `client.send`, a parent, a sub-agent's hand-back, plugins. `now` joins the running turn at its next step; `next_turn` waits for the turn to end. |
| `cancel` | `Cancel` | `reason` (str), `turn_only` (bool, default false) | `client.cancel`, a parent ending. `turn_only` stops just the current turn. |
| `answer` | `Answer` | `request_id` (str), `decision` (`allow` / `deny` / null), `always` (bool), `data` (any) | `client.answer`, with the dedupe key `answer:<request_id>`. For an approval, `data` `{"message": ...}` is what the person added; for a question, `data` is `{"answers": ...}` (`noeta/core/questions.py`). |
| `child_ended` | `ChildEnded` | `child_id`, `status` (`done` / `failed` / `cancelled`), `answer`, `error`, `last_usage` (its newest reply's usage), `tool_calls`, `output_tokens` | The child, in the same transaction as its own end, with the dedupe key `child:<child_id>`. |
| `timer` | `TimerFired` | `name` | The runner, for a due timer, with the dedupe key `timer:<name>:<seq that set it>`. |
| `external` | `External` | `name`, `data` | `client.signal`, plugins, channels. The name `config` changes the task's `model`, `effort`, `max_turns`, `output_schema`, and a few other keys from the next model call (`CHANGEABLE_CONFIG` in `noeta/core/loop/state.py`). |

## Core records

The kernel's own entries: `ExtensionRecorded(extension="core", kind=..., data=...)`, one typed
payload per kind (`noeta/core/records.py`). A plugin cannot write a `core` record of a kind the
kernel does not know. Front ends read `notice`, `retry`, `turn_failed`, `turn_stopped` and
`spawned`; the rest is the kernel's bookkeeping.

| `kind` | `data` | Written when |
|---|---|---|
| `notice` | `text`, optional `kind`, `data` | A plugin or tool calls `notify(...)`: something for the person, never the model. |
| `retry` | `attempt`, `delay`, `error` | A failed model call is about to be retried. |
| `continue` | optional `reason` (`empty_reply`), `model`, `usage` | The reply hit the output limit, or was empty and a plugin went on; its `message` asks the model to go on. `usage` is a dropped reply's. |
| `turn_stopped` | `partial`, `marker`, `thinking` (when stopped mid-reply) | The turn was stopped (`Cancel(turn_only=True)`). The streamed text is kept; the marker opens the person's next turn. |
| `turn_failed` | `reason`, `detail` | The turn could not go on; an interactive task waits for the next message. |
| `turn_done` | optional `reason` (`empty_reply`), `model`, `usage` | The turn ended without a reply: a `before_model` ended it (`/compact`), or the model replied with nothing. |
| `spent` | `model`, `usage`, `purpose` | Model calls made outside the conversation (a compaction's summarize call, a title): their cost counts in the task's usage. |
| `erased` | `seqs` | The person's messages at these seqs, and what was expanded from them, leave the conversation (a `UserPromptSubmit` hook blocked them). Every plugin reducer with state is folded with it as `core.erased`. |
| `parked` | `call_id`, `on`, `keys`, `detail` | A tool call waits on a question or on children. |
| `spawned` | `children`: each `{"id", "call_id", "background"}` | Child tasks this task staged. |
| `timer` | `name`, `at`, `text` | A timer is set (`set_timer`); one of the same name is replaced. |
| `timer_cleared` | `name` | A timer is cleared. |
| `prompts` | name → text | The prompt override files read when the task first started. |
| `plugins` | `pins`: each `{"name", "source", "version", "arguments"}` | The plugins the task runs with, on first start and whenever a pickup finds them changed. |

## Who writes what

Only the runner holding the task's claim appends events; the store writes `TaskCreated`. A
plugin never writes an event directly: what it records in a hook (`TaskView.record`,
`ToolContext.record`) is appended by the runner right after the hook, in the same append as the
event it belongs to. Everyone else, front ends included, posts inbox items.
