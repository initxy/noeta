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

Delivery is **single-worker durable exactly-once**. The matched wake is held
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

## What "single-host / single-worker" means

The guarantee above is scoped to the shipped deployment shape: one durable
store (SQLite) and one resident Worker process draining it. Within that
scope, a Worker crash at any point between match and consumption resolves to
exactly one durable `TaskWoken`. A crash **mid-step** — after `TaskWoken`,
before the step's remaining events land — recovers on the next lease: the
interrupted attempt is sealed with a durable `StepAttemptAbandoned` marker
and re-driven automatically when it recorded no side-effectful activity;
otherwise the Task is parked as a stopped conversation with a system notice
for a human to verify. One boundary remains open: multi-worker / multi-host
concurrency (fencing between competing Workers is not shipped). Both the
recovery scope and that boundary are catalogued in
[known limitations](../operations/limitations.md).

Related: [Task model](task-model.md) ·
[Engine & execution](engine-execution.md) ·
[Fold & snapshot](fold-and-snapshot.md)
