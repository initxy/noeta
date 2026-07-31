# Event sourcing: state = fold(log)

Noeta does not store "current state" as ground truth. A Task's ground truth is
its append-only **EventLog**; the state you want at any moment is the result of
folding that log:

> state = fold(the Task's events)

The state object is a disposable projection; the log is the master copy.
Everything Noeta advertises — durability, crash recovery, replay, audit — is a
consequence of this one decision, not a feature built next to it.

<p align="center">
  <img src="../assets/diagrams/event-sourcing.svg" alt="Event sourcing — EventLog + ContentStore → fold → four state slices" width="820">
</p>

## The EventLog

Each Task owns one append-only stream of `EventEnvelope` records. Every state
change emits an envelope: `TaskCreated`, `MessagesAppended`,
`ContextPlanComposed`, `ToolCallStarted`, `LLMRequestFinished`, `TaskSuspended`,
`TaskWoken`, `TaskCompleted`, and so on. There is no separate task table the
Engine reads.

An envelope carries the owning task, the event type, a typed payload, a monotonic
`seq`, a trace id, and an `origin` marker naming the role that wrote it
(`engine`, `llm`, `observer`, `tool`, `system`). The log assigns `seq` at append
time — callers hand it a placeholder — so each stream has exactly one
deterministic replay order.

Fold walks that order and routes each envelope to the handler registered for its
type. The handler table is deliberately not exhaustive: an unrecognized event
type is logged and skipped rather than raised, so a stream written by a producer
that knows more event types than the reader still folds.

## Large content lives beside the log

Envelope payloads are capped at 4 KB (`EVENT_PAYLOAD_MAX_BYTES`); a write over
the cap raises `PayloadTooLarge`. Anything larger — a full LLM request/response
body, a large tool output, a compaction summary, an oversized goal — goes to the
**ContentStore**, a content-addressed blob store that dedups by SHA-256; the
envelope carries only a `ContentRef(hash, size, media_type)`. Even a snapshot is
an ordinary event whose payload is a `state_ref`. The log stays a string of small
records, and "the log is the only ground truth" holds.

## The single-writer invariant

Fold can only promise "replaying the log yields exactly what ran" if nothing
changes state without going through the log first. Noeta enforces this by cutting
Task state into four typed slices — the rolling conversation stream, the Policy's
long-horizon memory, the composed-context slice, and the governance counters —
and nailing each slice to exactly one writer. The Policy, notably, cannot assign
to its own memory: it attaches a `TaskStatePatch` to the Decision it returns, the
Engine lands that as a `TaskStatePatched` event, and fold writes it back. The
full slice-by-writer breakdown is in the
[architecture overview](../architecture/overview.md).

## Why this matters

- **Durable by construction** — kill the process mid-task and fold brings the
  task right back. There is no separate "save" step to forget.
- **Reproducible** — the same log folds to byte-identical state in any process on
  any machine (see [Fold & snapshot](fold-and-snapshot.md)).
- **One mechanism, many uses** — recovering a task, showing it in a UI, and
  auditing it after the fact are all the same operation: a fold.

Related: [Task model](task-model.md) ·
[Fold & snapshot](fold-and-snapshot.md) ·
[Composer & cache](composer-and-cache.md)
