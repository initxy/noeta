# Wake & resume

A Task that is waiting does not block a thread — it **suspends**. Suspension is
one status with a typed `WakeCondition` on `wake_on`, whatever the reason for
waiting. The Task's state is safely in its EventLog; nothing about it lives in
process memory while it waits (see [Task model](task-model.md)).

<p align="center">
  <img src="../assets/diagrams/task-lifecycle.svg" alt="Task lifecycle — pending → running → suspended → terminal, with wake conditions" width="820">
</p>

## How a wake matches

Condition and event share one dataclass: the parent stores the shape it is
waiting for, and a producer later delivers the same type through
`Dispatcher.wake`. Matching is by **projection** — only identity fields
participate:

| Condition | Delivered by | Matches on |
| --- | --- | --- |
| `SubtaskCompleted` | `ChildLifecycleObserver` | `subtask_id` (the child's `result` rides along, informational) |
| `SubtaskGroupCompleted` | `ChildLifecycleObserver` | `group_id` (the member `subtask_ids` ride along) |
| `HumanResponseReceived` | the human-facing channel | `handle` |
| `TimerFired` | the Worker's timer poll | threshold: `event.fire_at >= condition.fire_at` |
| `ExternalEvent` | any external ingress | `event_kind` |

`matches_wake` is the single implementation of that truth table, and every
Dispatcher routes through it so no adapter can diverge in private. Cross-variant
matches are always false: a subtask wake cannot satisfy a timer condition no
matter what its fields say.

A match re-enqueues the Task. The next Worker to lease it receives the wake on
`Lease.wake_event`, the Engine writes a durable `TaskWoken` envelope, and the
step runs. Resuming is then just a fold — there is no separate recovery path
(see [Fold & snapshot](fold-and-snapshot.md)).

## The delivery guarantee

Delivery is **durable exactly-once**, assembled from at-least-once delivery plus
idempotent consumption. The matched wake is held durably by the Dispatcher and
outlives any individual lease: leasing does not consume it. It is cleared only
by a consuming release — `release(consumed_wake_event=…)` — which happens after
the `TaskWoken` envelope is safely in the log. If the Worker crashes after
leasing but before that write, the stale-lease sweep returns the Task to the
ready queue with its wake intact and the next lease delivers the same wake
again. Re-delivery is idempotent: the Worker looks for a `TaskWoken` matching
this wake inside the current suspend window, and if one already landed it
reconciles against the folded status instead of writing a second one.

Timer wakes have no external producer: the Worker calls
`Dispatcher.fire_due_timers(now=…)` on an interval, alongside the stale sweep,
and the Dispatcher flips every due timer suspension back to ready.

A suspended Task with no queued wake is not an error — it is simply waiting for
something that has not happened yet. The Worker re-releases it `suspended` with
`wake_on` preserved and emits a `suspended_without_wake` reliability signal:
process-local observability, not an EventLog event, and not a loss path.

## How far the guarantee scales

The guarantee holds for **many concurrent Workers**, not just one. A crash at
any point between match and consumption resolves to exactly one durable
`TaskWoken`, and competing Workers cannot both write: every lease-checked append
is fenced, so a stalled Worker whose lease was reclaimed is rejected rather than
allowed to land a write behind a later lease generation.

Two deployment scopes:

- **Single host, multiple Workers** — every backend. A host runs a resident
  `WorkerLoop` pool.
- **Multiple hosts** — Postgres, where the fence is an in-transaction
  `SELECT … FOR SHARE` on the dispatcher row inside the same transaction that
  inserts the event, with expiry compared against the database clock so per-host
  skew cannot split-brain. SQLite and in-memory are single-host by definition; a
  Worker pool on that one host is fine, but pointing two host processes at one
  SQLite file is not supported.

A crash **mid-step** — after `TaskWoken`, before the step's remaining events
land — recovers on the next lease. The interrupted attempt is sealed with a
durable `StepAttemptAbandoned` marker carrying the pre-attempt baseline, then
classified: an attempt that recorded no side-effectful activity is re-driven
automatically; anything else parks the Task as a stopped conversation with an
`origin="system"` notice, resting on the next-goal wake handle so typing resumes
it. Three consecutive seals in one window force a park regardless, so a crash
loop cannot retry forever.

The recovery scope, the SQLite single-host boundary, and the one remaining open
edge — sandbox side effects are not fenced across Worker generations — are
catalogued in [known limitations](../operations/limitations.md); the fencing
argument is in [multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)
and the seal-and-classify rules in
[step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md).

Related: [Task model](task-model.md) ·
[Engine & execution](engine-execution.md) ·
[Fold & snapshot](fold-and-snapshot.md)
