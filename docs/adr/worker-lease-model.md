# A worker leases one segment at a time: advance to the next suspend point and release, don't hold a task to completion

## Context

A task may run for hours. If a worker takes a lease and then holds the whole task to completion, the worker is locked onto one long task for hours: crashes are expensive, and scaling granularity is coarse. At the same time, waking a suspended task has several triggers (a HITL answer, a subtask completion, a timer expiry, an external webhook), and we must decide whether they each get their own protocol or converge onto one mechanism.

## Decision

Once a worker holds a lease, the Engine advances the Task to the **next suspend point or terminal**, then releases the lease — it does **not** hold the task to completion. A long-running task may run for hours, but each lease cycle only reaches the next suspend point (spawn_subtask / yield_for_human / wait_timer / wait_external / finish / fail). `Dispatcher.release(lease_id, next_state="suspended", suspend_reason=...)` explicitly declares whether the task is entering suspended or terminal.

- The Dispatcher protocol has **one** `wake(task_id, wake_event)`: HITL, subtask completion, timer expiry, and external webhook are all woken through this single mechanism — there are not four separate protocols.
- A worker must heartbeat to renew its lease (default lease 30s, heartbeat every 10s), with a hard renewal cap (default 1 hour) to prevent a stuck worker from deadlocking the task.
- A wake event may arrive early or late: if a wake arrives before the suspend, it is persisted into the task's `pending_wake_events`, and the suspend checks for it and immediately reschedules.
- A tool call must complete within a single lease (covered by heartbeat renewal); an extremely long tool should switch to the "start + wait_external" pattern.

## Rationale

- **Don't lock a worker onto a long task for hours.** One lease running the whole task is simpler, but crashing a worker is more expensive and scaling granularity is coarser. Leasing one segment at a time cuts worker occupancy to "the next suspend point," making both crash cost and scaling granularity controllable.
- **Use a single wake mechanism.** HITL / subtask completion / timer / webhook converge into a single `wake`, avoiding four parallel wake protocols each evolving separately.
- **Wake must tolerate arriving early or late**, or a race like "a subtask finishes before the parent task suspends" would lose the wake — `pending_wake_events` stashes an early wake and reschedules on suspend.

## Alternatives considered

1. **One lease runs the whole task** (`await engine.run(run)` all the way through). Rejected: a long task locks a worker for hours, crash cost is high, scaling granularity is coarse.
2. **One lease runs a single step.** Rejected: granularity is too fine — every step pays the lease + fold overhead, and workers are swapped repeatedly even while the task is in a tool loop, with poor performance.

## Consequences

- Load-bearing landing: `noeta.protocols.dispatcher` and `noeta.protocols.wake` are the `release(next_state=...)` and single-`wake` mechanism itself; the Engine's release path on reaching a suspend point / terminal, heartbeat renewal, and the early/late handling in `pending_wake_events` are its execution side.
- Constraint: a tool call must complete within a single lease, an extremely long tool must be split into "start + wait_external"; renewal has a hard cap, beyond which it is treated as stuck and the lease is abandoned.
