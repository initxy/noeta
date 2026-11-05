# A task is a log

Every noeta task is an append-only log of events. This page explains what goes in the log, how
the task's state and its next model request are computed from it, and how snapshots keep that
fast without ever becoming a second source of truth.

## Why a log

A task runs for minutes or days, calls tools with side effects, waits for people, and has to
survive crashes and move between processes and machines. Its state has to live somewhere: in a
process's memory, in rows updated in place, or in a log.

noeta keeps it in a log ([ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md)):

- The log holds a small, fixed set of event types (`noeta/core/events.py`).
- The task's state is a **fold** of the log: read the events in order, apply each one. No I/O,
  no clock, no randomness (`noeta/core/loop/state.py`).
- The next model request is a **pure function** of that state (`noeta/core/loop/request.py`).
- The row in the `tasks` table is a projection of the log, written in the same transaction as
  each append.

With that, crash recovery, continuing on another machine, forking a conversation, rewinding,
auditing and exact prompt-cache reuse are all the same operation: fold the log.

The alternatives were rejected for concrete reasons. Rows updated in place keep no history to
recover or fork from, and two writers race silently. A session in memory with periodic
checkpoints loses everything since the last checkpoint on a crash and runs side effects again.
Letting front ends append events directly gives no ordering against the runner and no dedupe;
they post to the inbox instead ([Waiting and waking](./waiting.md#the-inbox)).

## The ten event types

| Event | Written when |
|---|---|
| `TaskCreated` | The task is created (always seq 1): its agent, config, parent and root, and the conversation it continues, if any. |
| `InputTaken` | The runner takes items from the inbox: the items themselves and the last inbox seq taken. |
| `ModelCalled` | Just before a model call: the model, a hash of the request, the attempt number. |
| `ModelReplied` | The model's reply: the message (text, reasoning, tool calls), stop reason and token usage. |
| `ToolStarted` | Just before a tool call runs, with the call as it will run (after any rewrite). |
| `ToolFinished` | The call's result, plus data for front ends that the model never sees. |
| `Compacted` | The conversation up to a seq is replaced by a summary when requests are built. |
| `Waiting` | The task stops and waits: for input, an approval, a question's answer, children, a timer or an outside signal. |
| `TaskEnded` | The task is over: `done`, `failed` or `cancelled`. |
| `ExtensionRecorded` | A plugin's (or the kernel's) own durable entry, folded by that plugin. |

Every field of every event is in the [events reference](../reference/events.md).

There are deliberately few types. Anything a plugin needs to remember is an
`ExtensionRecorded(extension, kind, data)` entry, folded by that plugin's own `reduce`; the
kernel's own bookkeeping (a timer set, the plugins a task runs with, a turn that failed) uses the
same event under `extension="core"`, with one typed payload per kind in
`noeta/core/records.py`. Adding a capability never adds an event type, so an older front end can
always read a newer task's log.

## What a log looks like

Here is a real log, printed from a run with a scripted model and the file tools: the person asks
what is in a file, the model reads it with one tool call and answers.

<Diagram name="core-event-log" />

```text
 1 TaskCreated        agent=demo
 2 ExtensionRecorded  core/prompts   {}                      prompt override files found at start
 3 ExtensionRecorded  core/plugins   {pins: [...]}           the plugins the task runs with
 4 InputTaken         items=[Say "what is in a.txt?"]  through=1
 5 ModelCalled        model=m  request_hash=372920a9…
 6 ModelReplied       tool_use Read {file_path: a.txt}   stop_reason=tool_use
 7 ToolStarted        Read {file_path: a.txt}
 8 ExtensionRecorded  fs/read        {path, digest, mtime, …}  the file tools note what was read
 9 ToolFinished       "1\thello world"
10 ModelCalled        model=m  request_hash=223cf056…
11 ModelReplied       "It says hello world."   stop_reason=end_turn
12 Waiting            on=input  detail={answer: "It says hello world."}
```

A few things to notice:

- **The inbox item is copied into the log.** `InputTaken` carries the whole message, not a
  reference to the inbox. The log alone says everything the task saw.
- **Seq 2 and 3 are written on the first pickup**, before the first step: the prompt override
  files and the plugin versions in use. Recording them is what lets a later worker send the same
  bytes and refuse to run without one of those plugins; a pickup that finds them changed records
  them again ([plugin pins](./recovery.md#plugin-pins)).
- **A plugin's record lands with the result.** Seq 8 is the file tools' note of what `Read`
  read (so a later `Edit` can check the file has not changed since), written in the same append
  as the `ToolFinished` after it.
- **`ModelCalled` comes before the call, `ToolStarted` before the tool.** That ordering is what
  makes crash recovery possible ([recovery](./recovery.md)).
- **`Waiting` ends the run.** Once the plugins have seen the `Waiting` and their records have
  landed, the worker gives the claim back (unless a background job keeps it,
  [kept claims](./waiting.md#kept-claims)); the task then holds no worker until something
  arrives in its inbox.

A longer task looks the same, with more of the middle: reminders and plugin notes appear as
`ExtensionRecorded` entries (some carrying a `message` that joins the conversation), parallel
read-only calls appear as several `ToolStarted` in one append followed by their `ToolFinished`
as each completes, and a compaction appears as one `Compacted`.

## The fold

`apply(state, recorded, reducers)` in `noeta/core/loop/state.py` folds one event into a
`TaskState`. `fold` applies them all. The state holds what the next step needs:

- `messages` — the conversation as the model sees it (after compaction), and the log seq that
  produced each one;
- `calls` — the latest reply's tool calls that are not all answered yet, each marked started,
  finished, or parked;
- `turn_open`, `waiting`, `answers`, `spawned` / `children`, `timers`, `queued` messages;
- counters: `steps_in_turn`, `model_attempts`, `usage`, `tool_calls`;
- `ext` — each plugin's own state, by plugin name, built by its `reduce`.

Then `next_action(state, one_shot=..., max_steps=...)` in `noeta/core/loop/decide.py` decides
what the runner does next (call the model, run calls, recover interrupted calls, wait, end...)
from the state and two of the agent's settings, nothing else.
[One step of a task](./one-step.md) walks through it.

The fold has no I/O. `pyproject.toml` has an import contract for it: `noeta.core.loop` may not
import the runner, the store or the provider. The same events always give the same state; that
is the whole basis of recovery.

A plugin's `reduce(state, kind, data)` must be pure in the same way. It sees only its own
records (and the records of plugins it declares in `Extension.folds`), plus one kernel kind,
`core.erased`, when the person's messages are erased from the conversation (a blocked prompt,
[ADR-026](https://github.com/initxy/noeta/blob/main/docs/adr/026-a-blocked-prompt-is-erased-by-a-core-record.md)).

## The request is a function of the state

`build_request(state, ...)` in `noeta/core/loop/request.py` builds the model request:

- the system prompt as two cached blocks, as Claude Code sends it: the agent's identity line,
  then its prompt with each plugin's context parts placed in order;
- the messages, exactly `state.messages`;
- the tools, sorted by name, with server tools last and deferred tools marked;
- one cache breakpoint, on the newest message.

Everything that goes into it either comes from the fold or is fixed for the task's life and
recorded on the first pickup (prompt overrides, `core/prompts`). Reminders, the text a plugin
adds before a model call, are recorded in the log before `ModelCalled`. So a worker that picks
the task up after a crash, on another machine, builds the same bytes, and the provider's prompt
cache keeps hitting. `ModelCalled.request_hash` is the hash of what was sent, so a mismatch is
visible. How the request is laid out for caching is on
[Context, caching and compaction](./context.md).

## The log is the only full transcript

Since [ADR-030](https://github.com/initxy/noeta/blob/main/docs/adr/030-the-log-is-the-only-full-transcript.md)
the fold keeps only what the next step needs. After a compaction, `state.messages` is the
summary plus what follows it, not every message ever sent. The log keeps everything.

<Diagram name="core-log-vs-fold" />

- A saved transcript, an audit or a rewind reads the log
  (`noeta/plugins/compaction/transcript.py` refolds it, following `history_from` into a source
  task's log).
- A plugin that needs a fact from before a compaction records it in its own state when it
  happens (the `tool_search` plugin's loaded tools, the memory plugin's pages read), written in
  the same append as the tool result.
- The one per-call growth left in the fold is `TaskState.call_ids` (tens of bytes per call),
  which keeps tool-call ids unique across the task even when a gateway reuses them.

The alternative, keeping a full history in the fold, made every snapshot grow with the task's
age: a test task with 14 compactions had a 255 KB snapshot; now it is 5.9 KB.

## Continuing another task's conversation

`TaskCreated.history_from` starts a task from another task's conversation: that task's log up to
`history_through` is folded first, then the new task's own events. That is how a fork, a rewind
and a resumed sub-agent work. Creating the task pins an unset `history_through` to the source's
last seq at that moment (`noeta.core.store.pin_history`), so the source growing later never
changes what the new task folds to.

The source's state is carried over through `continued()`: its compacted conversation and each
plugin's state (through `Extension.continue_state`), nothing tied to the source's run (its
inbox, timers, children, open turn). Calls still open in the source get a result in the copy
saying they ran in the original conversation.

## Where large content goes

There is no separate blob store. Events are stored whole, as msgspec JSON bytes, in the store's
`events` table (`bytea` on Postgres, so text with `\u0000` from a tool cannot fail an append).

What keeps the log small is the large-output rule, which is the kernel's
(`noeta/core/large_output.py`): a tool result over its limit (50,000 characters by default, and
200,000 for all of one reply's results together) is saved whole to a file in the task's own
directory below the spill root, written through the task's exec env, and the log gets a preview
and the path. The model reads the rest with `Read` if it needs it, on whichever worker runs the
task next. See [Context, caching and compaction](./context.md).

## Snapshots

Folding a long log on every claim gets slow: a task is claimed again after every wait, crash or
move, so the cost would grow with the task's age. A **snapshot** is a task's folded state as of
one seq, stored so a claim folds the snapshot plus the events after it
([ADR-006](https://github.com/initxy/noeta/blob/main/docs/adr/006-fold-snapshots.md)).

<Diagram name="core-snapshot" />

**When one is written.** After an append (or right after a claim's load), once the log is at
least `Runtime.snapshot_every` events past the newest snapshot (default 100;
`noeta worker --snapshot-every`, `0` never) *and* those events' encoded bytes are at least a
tenth (`SNAPSHOT_GROWTH`) of the snapshot's size (`noeta/core/runner/snapshotting.py`). Tying the interval to the snapshot's size makes the sizes
grow geometrically, so the bytes written stay within about 11 times the log's, while a claim
folds at most a tenth of the snapshot's bytes in events. With a fixed interval, a 21,627-event
log wrote 699 MiB of snapshots (91 times the log); with this rule, 75 MiB
([ADR-012](https://github.com/initxy/noeta/blob/main/docs/adr/012-snapshots-follow-state-size.md)).

**How.** In the background, folded from the *stored* events after the previous snapshot, never
from the runner's live state, so a snapshot is exactly what a fold of the log gives. A state is
stored only if it decodes back equal (`snapshots.encode`); a plugin state the encoding cannot
carry faithfully means no snapshot for that task, never a wrong one. Writing one needs no claim:
it is a fold of a log prefix, which never changes.

**Versioned by the code that folds.** A snapshot's version is a hash of the kernel's fold
modules (`events`, `records`, `types`, `loop/state`, and everything in noeta they import), the
agent's name, each plugin's pin (name, source, version or file hash, arguments) and the source
of each plugin's classes and the modules they import (`noeta/core/snapshots.py`). Source is
hashed once per process (when the worker starts, or first meets an agent), so a code upgrade on
disk does not change what a running worker stamps. A worker uses only a snapshot of its own version; anything else and it folds the log
from the start. An upgrade or a plugin change can only make a snapshot unused, never apply stale
fold rules. A manual version number was rejected because forgetting to bump it yields a wrong
fold.

**Two versions at once.** A store keeps at most `SNAPSHOT_VERSIONS` (2) per task, one per fold
version, so during a rolling upgrade workers of two versions do not overwrite each other's
([ADR-023](https://github.com/initxy/noeta/blob/main/docs/adr/023-snapshots-per-fold-version-and-newer-logs-left-to-newer-workers.md)).

**Deleted at the end.** The append that ends a task deletes its snapshots in the same
transaction, and `put_snapshot` keeps none for an ended task: nothing claims an ended task again.

A snapshot **may be missing, but never wrong**. The test suite enforces it: every task log any
test loads is folded both ways, from the start and from snapshots taken at several seqs, and
the two are compared (`tests/conftest.py`, `tests/snapshot_check.py`).

Measured on a 20,002-event log, a claim went from 120 ms to 21 ms on sqlite and from 197 ms to
119 ms on Postgres.

## When a log cannot be read

- **A newer version wrote it.** During a rolling upgrade an old worker may meet a record kind,
  tag or value only a newer version writes (msgspec's "Invalid value", or a `core` record of an
  unknown kind, `records.NewerRecord`). Skipping it could fold a different task, so the worker
  writes nothing and lets its lease run out; a newer worker claims the task sooner or later.
  The same holds for an inbox item of a newer version.
- **It is damaged.** A log (or an inbox item) no claim can read or fold for any other reason ends the task
  `failed` with `unreadable_log` after `LOAD_ATTEMPTS` (3) such claims in a row on one worker,
  written from the task row since there is no folded state. Its parent hears it; its children
  are cancelled.
- **A snapshot will not decode.** It is ignored and the log is folded from the start.

A plugin cannot cause the first case by accident: `ToolContext.record` and `TaskView.record`
refuse a `core` record of a kind the kernel does not know (`records.check_writable`).

## Related

- [One step of a task](./one-step.md): how the runner appends these events.
- [One writer and crash recovery](./recovery.md): why only one worker can append.
- [Context, caching and compaction](./context.md): what `Compacted` does and how the request is cached.
- [Events reference](../reference/events.md): every field.
