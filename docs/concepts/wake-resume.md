# Wake & resume

Agents spend most of their life waiting — for a person to answer, for a
subtask to finish, for a timer, for something outside the system. In Noeta a
waiting Task holds no thread, no connection, and no process memory. It
**suspends**: it records what it is waiting for and lets go of execution
entirely.

Later, something matching that condition arrives, the Task is re-enqueued, and
a Worker picks it up and continues. Because all its state is folded back out of
its EventLog, resuming needs no special recovery code — it is the same fold
every leased step already does (see [fold & snapshot](fold-and-snapshot.md)).

<p align="center">
  <img src="../assets/diagrams/wake-resume.svg" alt="Wake and resume — a task suspends on a wake condition, a matching wake event arrives, the match is held durably, and the task resumes" width="820">
</p>

## Where this sits in a Task's life

Suspension is one of the four statuses, and it is the only one that means
"waiting" — no matter what is being waited on:

<p align="center">
  <img src="../assets/diagrams/task-lifecycle.svg" alt="Task lifecycle — pending → running → suspended → terminal" width="820">
</p>

One status and one resume path for four different kinds of waiting is a
deliberate simplification. It is why adding, say, a new external trigger does
not add a new lifecycle branch.

## What a Task can wait for

The four kinds of waiting map to five condition dataclasses — a subtask wait
comes in a single-child and a group variant. The condition a Task stores and
the event that later satisfies it are the *same dataclass*. Stored on `wake_on` it declares the shape being waited for;
delivered through `Dispatcher.wake` it carries the answer.

| Condition | Delivered by | Matches on |
| --- | --- | --- |
| `SubtaskCompleted` | `ChildLifecycleObserver` | `subtask_id` (the child's `result` rides along, informational) |
| `SubtaskGroupCompleted` | `ChildLifecycleObserver` | `group_id` (the member `subtask_ids` ride along) |
| `HumanResponseReceived` | the human-facing channel | `handle` |
| `TimerFired` | the Worker's timer poll | threshold: `event.fire_at >= condition.fire_at` |
| `ExternalEvent` | any external ingress | `event_kind` |

Matching is by **projection**: only the identity fields in the right-hand
column participate. Payload fields such as a `SubtaskResult` travel along for
information and never affect whether the match happens.

`matches_wake` is the single implementation of that truth table, and every
Dispatcher routes through it, so no adapter can quietly diverge. Cross-variant
matches are always false — a subtask wake cannot satisfy a timer condition, no
matter what its fields say.

## What happens on a match

A match re-enqueues the Task. The next Worker to lease it receives the wake on
`Lease.wake_event`, the Engine writes a durable `TaskWoken` envelope, and the
step runs.

## The delivery guarantee

Delivery is **durable and exactly-once**, assembled from at-least-once delivery
plus idempotent consumption.

The matched wake is held durably by the Dispatcher and outlives any individual
lease — leasing does not consume it. It is cleared only by a *consuming*
release (`release(consumed_wake_event=…)`), which happens after the `TaskWoken`
envelope is safely in the log.

So consider a Worker that crashes after leasing but before that write. The
stale-lease sweep returns the Task to the ready queue with its wake intact, and
the next lease delivers the same wake again. Re-delivery is idempotent: the
Worker looks for a `TaskWoken` matching this wake inside the current suspend
window, and if one already landed it reconciles against the folded status
instead of writing a second one. Either way, exactly one `TaskWoken` ends up on
the stream, and nobody has to intervene by hand.

Two supporting details:

- **Timers have no external producer.** The Worker calls
  `Dispatcher.fire_due_timers(now=…)` on an interval, alongside the stale
  sweep, and the Dispatcher flips every due timer suspension back to ready.
- **A suspended Task with no queued wake is not an error.** It is simply
  waiting for something that has not happened yet. The Worker re-releases it
  `suspended` with `wake_on` preserved and emits a `suspended_without_wake`
  reliability signal — process-local observability, not an EventLog event.

## How far the guarantee scales

It holds for **many concurrent Workers**, not just one. A crash at any point
between match and consumption resolves to exactly one durable `TaskWoken`, and
competing Workers cannot both write: every lease-checked append is fenced, so a
stalled Worker whose lease was already reclaimed is rejected rather than
allowed to land a write behind a later lease generation.

Two deployment scopes:

- **Single host, multiple Workers** — every backend supports this. The host
  runs a resident `WorkerLoop` pool.
- **Multiple hosts** — Postgres. The fence is an in-transaction
  `SELECT … FOR SHARE` on the dispatcher row inside the same transaction that
  inserts the event, with expiry compared against the database clock so
  per-host clock skew cannot split-brain. SQLite and in-memory are single-host
  by definition: a Worker pool on that one host is fine, but pointing two host
  processes at one SQLite file is not supported.

## Crashing mid-step

A crash *after* `TaskWoken` but before the step's remaining events land is
handled one level down. On the next lease the interrupted attempt is sealed
with a durable `StepAttemptAbandoned` marker carrying the pre-attempt baseline,
then classified:

- an attempt that recorded no side-effectful activity is re-driven
  automatically;
- anything else parks the Task as a stopped conversation with an
  `origin="system"` notice, resting on the next-goal wake handle, so typing
  again resumes it.

Three consecutive seals inside one window force a park regardless, so a crash
loop cannot retry forever.

The recovery scope, the SQLite single-host boundary, and the one remaining open
edge — sandbox side effects are not fenced across Worker generations — are
catalogued in [known limitations](../operations/limitations.md). The fencing
argument lives in
[multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)
and the seal-and-classify rules in
[step-attempt recovery](https://github.com/initxy/noeta/blob/main/docs/adr/step-attempt-recovery.md).

## Next

- [The Task model](task-model.md) — the statuses this page moves between.
- [Fold & snapshot](fold-and-snapshot.md) — how a resumed Task gets its state
  back.
- [Deploy a worker](../how-to/deploy-worker.md) — running the loop that leases,
  sweeps, and fires timers.
- [Worker loop](../reference/worker-loop.md) — the knobs on `WorkerLoop`.
