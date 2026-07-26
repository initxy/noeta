# Wake & resume

A Task that is waiting does not block a thread — it **suspends**. Suspension
is one status with a typed `WakeCondition` attached, whatever the reason for
waiting: `SubtaskCompleted` (a spawned Subtask finishing),
`HumanResponseReceived` (an answer or approval), or `TimerFired` (a scheduled
wake). The Task's state is safely in its EventLog; nothing about it lives in
process memory while it waits (see [Task model](task-model.md)).

## How a wake matches

When a wake event arrives, the Dispatcher matches it against suspended Tasks
by **projection**: only identity fields participate in the match —
`subtask_id` for subtasks, `handle` for human responses, and `fire_at` for
timers, with threshold semantics (`event.fire_at >= condition.fire_at`). A
match re-enqueues the Task; the next Worker to lease it receives the wake
event alongside the Lease, and the Engine writes a durable `TaskWoken`
envelope before the Task continues. Resuming is then just a fold — there is
no separate recovery path (see [Fold & snapshot](fold-and-snapshot.md)).

## The delivery guarantee

Delivery is **durable exactly-once**. The matched wake is held
durably by the Dispatcher and outlives any individual lease: it is cleared
only when a step consumes it, which happens after the `TaskWoken` envelope is
safely in the log. If the Worker crashes after leasing but before that write,
the stale-lease sweep returns the Task to the ready queue with its wake
intact, and the next lease delivers the same wake again. Re-delivery is
idempotent: if the `TaskWoken` envelope already landed, the Worker reconciles
against it instead of writing a second one. No manual intervention is needed
in either direction — the wake fires once, durably, on its own.

A suspended Task with no queued wake is not an error: it is simply waiting
for something that has not happened yet. Inspecting such a Task reports a
typed `suspended_without_wake_event` — a diagnostic, not a failure. (The full
crash-recovery machinery behind this guarantee is described in the
[architecture overview](../architecture/overview.md).)

## How far the guarantee scales

The guarantee holds for **many concurrent Workers**, not just one. A Worker
crash at any point between match and consumption resolves to exactly one
durable `TaskWoken`, and competing Workers cannot both write: every
lease-checked append is fenced, so a stalled Worker whose lease was reclaimed
is rejected rather than allowed to land a write behind the new generation.

Two deployment scopes:

- **Single host, multiple Workers** — shipped on every backend. The platform
  runs a resident pool by default (`AGENT_NUM_WORKERS`, default 4).
- **Multiple hosts** — shipped on **Postgres**, where the fence is an in-transaction
  row-share check against the database clock (so per-host clock skew cannot
  split-brain). SQLite and in-memory are single-host by definition; a Worker
  pool on that one host is fine, but pointing two host processes at one SQLite
  file is not supported.

A crash **mid-step** — after `TaskWoken`, before the step's remaining events
land — recovers on the next lease: the interrupted attempt is sealed with a
durable `StepAttemptAbandoned` marker and re-driven automatically when it
recorded no side-effectful activity; otherwise the Task is parked as a stopped
conversation with a system notice for a human to verify.

The recovery scope, the SQLite single-host boundary, and the one remaining open
edge (sandbox side effects are not fenced across Worker generations) are
catalogued in [known limitations](../operations/limitations.md); the fencing
design is in the multi-host lease fencing ADR.

Related: [Task model](task-model.md) ·
[Engine & execution](engine-execution.md) ·
[Fold & snapshot](fold-and-snapshot.md)
