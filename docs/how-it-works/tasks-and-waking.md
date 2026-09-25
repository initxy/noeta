# Tasks and waking

A **Task** is one run of an agent and the only unit of work Noeta has. A chat that
lasts for weeks, a nightly job and a delegated subagent are all Tasks. When a Task
has to wait — for a person, a timer, a subtask or an outside event — it
**suspends**, holds nothing while it waits, and is woken exactly once when the
thing it waits for arrives.

<NtTaskStates />

## What it guarantees

- **One status for all waiting.** Every kind of wait is `suspended` plus a typed
  wake condition, so there is one resume path, not four.
- **Waiting is free.** A suspended Task holds no thread, connection or process
  memory. It can wait for seconds or months.
- **Exactly-once durable wake.** A matched wake survives worker crashes and is
  consumed exactly once.
- **Single writer.** A worker holds a lease on a Task while it runs; a worker
  whose lease was reclaimed cannot write.

## The four statuses

| Status | Meaning |
| --- | --- |
| `pending` | created, or woken, and waiting for a worker |
| `running` | a worker holds the lease and the Engine is advancing it |
| `suspended` | waiting on a wake condition recorded in `wake_on` |
| `terminal` | ended with `TaskCompleted`, `TaskFailed` or `TaskCancelled` |

A multi-turn conversation is one Task: each turn is wake → a few steps →
suspend, and between turns the Task rests at `suspended` waiting for the next
human message; only `query()` (and any `Client` built with `multi_turn=False`) ends
with `TaskCompleted`. Any status that has not ended can be cancelled. There is no separate session or workflow object; a host that wants
a user-facing "session" builds it on top.

## What a Task can wait for

The condition a Task stores and the event that satisfies it are the same
dataclass. Only the identity fields decide a match; payloads ride along.

| Condition | Delivered by | Matches on |
| --- | --- | --- |
| `SubtaskCompleted` | `ChildLifecycleObserver` | `subtask_id` |
| `SubtaskGroupCompleted` | `ChildLifecycleObserver` | `group_id` |
| `HumanResponseReceived` | your human-facing channel | `handle` |
| `TimerFired` | the worker's timer poll | `event.fire_at >= condition.fire_at` |
| `ExternalEvent` | any external source | `event_kind` |

`matches_wake` is the single implementation every dispatcher uses, so storage
backends cannot disagree about what matches.

## How a wake is delivered

<NtWake />

1. A wake event arrives through `Dispatcher.wake` (a timer through
   `Dispatcher.fire_due_timers`). If it matches, the dispatcher stores the match
   and puts the Task back on the ready queue.
2. The next worker to lease the Task receives the wake on `Lease.wake_event`.
3. The Engine writes a `TaskWoken` event. That write is the commit point.
4. Only then does the worker release with the wake marked consumed.

If a worker dies between steps 2 and 4, the stored match is still there; the stale
sweep requeues the Task and the next lease delivers the same wake. If `TaskWoken`
had already landed, the worker sees it and does not write a second one.
At-least-once delivery plus idempotent consumption gives exactly-once.

Timers need no outside producer: each worker calls
`Dispatcher.fire_due_timers(now=…)` on an interval, next to the stale sweep. A
suspended Task with no queued wake is not an error — it is simply still waiting.

## Scale

| Deployment | Supported |
| --- | --- |
| One host, a pool of workers | every backend (in-memory, SQLite, Postgres) |
| Several hosts sharing one store | Postgres only; the lease check runs inside the insert transaction against the database clock |

Pointing two host processes at one SQLite file is not supported.

## Subtasks

A subtask is an ordinary Task with its own log, related to its parent only by
`parent_task_id`; `subtask_depth` is capped by the budget so delegation cannot
recurse forever. The parent suspends on `SubtaskCompleted` (or
`SubtaskGroupCompleted` for a fan-out) and each child's result comes back as the
wake. Every node recovers on its own. The root of the tree, `root_task_id`, owns
anything that outlives a step: background shells, background subagents, a
sandbox container.

## What this means for you

- Ask a human, wait on a timer or delegate without holding anything open; the
  answer can arrive days later on a different machine.
- Run a `WorkerLoop` pool so someone leases, sweeps and fires timers — see
  [Deploy](../guides/deploy.md) and the [worker loop reference](../reference/worker-loop.md).
- Use Postgres as soon as more than one host process shares the work.

Design records:
[task as the only primitive](https://github.com/initxy/noeta/blob/main/docs/adr/task-as-only-primitive.md) ·
[durable wake](https://github.com/initxy/noeta/blob/main/docs/adr/subtask-fanout-and-durable-wake.md) ·
[multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)

## Next

- [The engine](engine.md) — what a Task does while it is `running`.
- [Delegate to subagents](../guides/subagents.md) — parents and children in practice.
- [The event log](event-log.md) — how a woken Task gets its state back.
