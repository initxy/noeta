# Event sourcing: state = fold(log)

Most systems keep a task's current state somewhere — a row, a document, a blob
— and update it in place. Noeta does the opposite. Everything that happens to a
Task is appended to that Task's own **EventLog**, and the state you want is
recomputed from the log whenever you ask for it. The log is the master copy;
the state object is a disposable projection you can throw away and rebuild.

That rebuild step is called a **fold**:

> state = fold(the Task's events)

<p align="center">
  <img src="../assets/diagrams/event-sourcing.svg" alt="Event sourcing — events append to the EventLog, large bodies go to the ContentStore, fold rebuilds the four state slices" width="820">
</p>

Durability, crash recovery, replay, and audit are all consequences of this one
decision — not features bolted on beside it.

## What the log actually holds

Each Task owns one append-only stream of records. Here is the opening of a
typical stream, one line per record:

| `seq` | type | what it records |
| --- | --- | --- |
| 1 | `TaskCreated` | the immutable header — goal, agent name, parent task |
| 2 | `MessagesAppended` | the user's message |
| 3 | `ContextPlanComposed` | a reference to exactly what the model was shown |
| 4 | `LLMRequestFinished` | the model's reply and its token usage |
| 5 | `ToolCallStarted` | `read(path="README.md")` |
| 6 | `MessagesAppended` | the tool's result, back in the conversation |
| … | | the loop continues |
| 41 | `TaskSuspended` | the Task is waiting for something |
| 42 | `TaskWoken` | the thing it was waiting for arrived |
| 58 | `TaskCompleted` | the final answer |

There is no separate task table the Engine reads. If it is not on the stream,
it did not happen.

## What one record looks like

Every record is an `EventEnvelope`. Beyond the typed payload it carries `seq`
(the position in the stream), `type`, `actor` (who wrote it), `trace_id`,
`causation_id`, and `origin` — a marker naming the *role* that appended it, one
of `engine` / `llm` / `observer` / `tool` / `system`.

The log assigns `seq` itself at append time; callers hand it a placeholder.
That is what gives each stream exactly one deterministic replay order.

Fold walks that order and routes each envelope to the handler registered for
its type. The handler table is deliberately not exhaustive: an unrecognized
type is logged and skipped rather than raised, so a stream written by a newer
producer still folds in an older reader.

## Large bodies live beside the log

Envelope payloads are capped at 4 KB (`EVENT_PAYLOAD_MAX_BYTES`); a write over
the cap raises `PayloadTooLarge`. Anything bigger goes to the **ContentStore**,
a content-addressed blob store that deduplicates by SHA-256, and the envelope
carries only a `ContentRef(hash, size, media_type)` pointing at it.

A full LLM request body, a large tool output, a compaction summary, an
oversized goal — all of them take this route. So does a snapshot: it is an
ordinary event whose payload is just a `state_ref`. The log stays a string of
small records, and "the log is the only ground truth" keeps holding.

## Only one writer per slice

Fold can only promise "replaying the log yields exactly what ran" if nothing
changes state without going through the log first. Noeta enforces that by
cutting Task state into four typed slices and nailing each slice to exactly one
writer — see [the Task model](task-model.md) for the slices themselves and
[state and writers](../architecture/state-and-writers.md) for the full table.

The Policy is the clearest case. It is the component that decides what the
agent does next, and it *cannot* assign to its own memory slice. Instead it
attaches a `TaskStatePatch` to the decision it returns, the Engine lands that as
a `TaskStatePatched` event, and fold writes it back. Wanting to change state and
being allowed to record it are two different rights, held by two different
components.

## What this buys you

- **Durable by construction.** Kill the process mid-task and a fold brings the
  Task right back. There is no separate "save" step anyone can forget.
- **Reproducible.** The same log folds to byte-identical state in any process
  on any machine — see [fold & snapshot](fold-and-snapshot.md).
- **One mechanism, many uses.** Recovering a crashed Task, rendering it in a
  UI, and auditing it a month later are the same operation: a fold.
- **Nothing is scrubbed.** Corrections are new events, not edits. Even a rewind
  appends a marker that names a new baseline; the old records stay on the
  stream.

## Next

- [The Task model](task-model.md) — what a Task is, and the four state slices
  fold writes into.
- [Fold & snapshot](fold-and-snapshot.md) — the fold function itself, and how
  snapshots keep it fast.
- [State and writers](../architecture/state-and-writers.md) — the slice-by-writer
  table and the versioned-fold rules.
- [SDK types](../reference/sdk-types.md) — the event and message types you get
  back from `Client.events` and `Client.messages`.
