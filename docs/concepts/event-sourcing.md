# Event sourcing: state = fold(log)

Noeta does not store "current state" as ground truth. A task's ground truth is
its append-only **EventLog**; the state you want at any moment is the result of
folding that log from the beginning:

> state now = fold(all events from creation to now)

The state object is a disposable projection; the log is the master copy.
Everything Noeta advertises — durability, crash recovery, replay, audit — is a
consequence of this one decision, not a feature built next to it.

## The EventLog

Each Task owns one append-only stream of `EventEnvelope` records. Every state
change emits an envelope: `TaskCreated`, `MessagesAppended`,
`LLMRequestStarted`, `ToolCallStarted`, `TaskSuspended`, `TaskWoken`,
`TaskCompleted`, and so on. There is no separate "task table" the Engine
reads — the log is the single source of truth.

An envelope carries the owning task, the event type, a typed payload, and a
monotonic sequence number. The sequence is assigned by the log at write time,
not by the caller, giving each stream a deterministic replay order: fold is
exactly "feed each payload to its handler in ascending sequence order."

## Large content lives beside the log

Envelope payloads are capped at 4 KB. Anything larger — a full LLM
request/response body, a large tool output — goes to the **ContentStore**, a
content-addressed, dedup-by-hash blob store; the envelope carries only a
`ContentRef(hash, size, media_type)`. Even a snapshot is an ordinary event
whose payload is a reference. The log stays a string of small records, and
"the log is the only ground truth" is never diluted.

## The single-writer invariant

Fold can only promise "replaying the log yields exactly what ran" if nothing
changes state without going through the log first. Noeta enforces this by
cutting task state into four slices — the conversation stream, the Policy's
long-horizon memory, the context plan, and the governance counters — and
nailing each slice to exactly one writer. The Policy, notably, cannot assign
to its own memory directly: it attaches a state patch to the Decision it
returns, the Engine lands that as an event, and fold writes it back. The full
slice-by-writer breakdown is in the
[architecture overview](../architecture/overview.md).

## Why this matters

- **Durable by construction** — kill the process mid-task and fold brings the
  task right back. There is no separate "save" step to forget.
- **Reproducible** — the same log folds to byte-identical state in any process
  on any machine (see [Fold & snapshot](fold-and-snapshot.md)).
- **One mechanism, many uses** — recovering a task, showing it in a UI, and
  auditing it after the fact are all the same operation: a fold.

Related: [Task model](task-model.md) ·
[Fold & snapshot](fold-and-snapshot.md) ·
[Composer & cache](composer-and-cache.md)
