# A worker leases one segment at a time: advance to the next suspend point and release, never hold a task to completion

## Context

A task may run for hours. A worker that takes a lease and holds it until the task finishes is locked onto one long task: a crash is expensive, and scaling granularity is coarse. Independently, waking a suspended task has several triggers — a human answer, a subtask completion, a timer expiry, an external webhook — and either each gets its own protocol or they converge on one.

## Decision

Once a worker holds a lease, the Engine advances the Task to the **next suspend point or terminal**, then releases the lease. A task may live for hours, but each lease cycle only reaches the next suspend point: spawn subtask, yield for human, wait timer, wait external, finish, or fail. `Dispatcher.release` declares explicitly whether the task is entering `suspended` or `terminal`.

- The Dispatcher Protocol has **one** `wake(task_id, wake_event)`. Human answers, subtask completions, timer expiries, and external webhooks all arrive through it — not four parallel protocols.
- A worker heartbeats to extend its lease deadline. Renewals are capped (`heartbeat_max`, default 360): past the cap the heartbeat raises `InvalidLease` and the dispatcher force-releases the task to `suspended` with reason `lease_quota_exceeded`, so a wedged worker cannot hold a task forever.
- A wake may arrive early or late. An event that arrives before the suspend is buffered in the dispatcher's per-task pending-wake queue; the suspend checks that queue and reschedules immediately when one matches.
- A lease can also be **yielded** back to the ready queue without a state transition, which is how a seeded task is handed from the request path to the resident worker.
- A tool call must complete within one lease, which heartbeat renewal covers. A tool that can outlast the renewal cap belongs in the "start, then wait external" shape instead.

## Rationale

- **Leasing a segment at a time bounds worker occupancy at the next suspend point**, which makes both crash cost and scaling granularity controllable rather than task-sized.
- **One wake mechanism.** Four triggers converging on a single `wake` avoids four wake protocols evolving apart, each with its own delivery and dedup story.
- **Wake must tolerate arriving early or late**, or a race such as a subtask finishing before its parent suspends loses the wake outright. Buffering an early wake and draining it at suspend closes that race.

## Alternatives considered

1. **One lease runs the whole task to completion.** Rejected: a long task locks a worker for hours, a crash costs the whole task, and scaling granularity is the task rather than the segment.
2. **One lease runs a single step.** Rejected: too fine. Every step pays lease acquisition plus a fold, and workers are swapped repeatedly even while the task sits in a tool loop.

## Consequences

- The mechanism is the `release(next_state=...)` shape and the single `wake` in `noeta.protocols.dispatcher` and `noeta.protocols.wake`; the execution side is the Engine's release on reaching a suspend point or terminal, the heartbeat side-thread and stale sweep in `noeta.runtime.worker`, and each dispatcher adapter's pending-wake handling.
- A poison task must not requeue forever: each stale-lease reclamation increments a per-task reclaim counter, and at `reclaim_max` (default 3) consecutive no-progress reclamations the sweep drops the task to terminal with reason `stale_reclaim_exceeded`. The counter resets on real progress — a heartbeat, a clean release, a controlled fail-requeue, or a fresh enqueue. Taking a lease is not progress. Every dispatcher adapter implements this identically.
