# WorkerLoop

`WorkerLoop` (exported from `noeta.sdk`) leases a ready task, advances it one step, releases it, and repeats — with heartbeats, stale-lease sweeps, timer polling and a bounded shutdown.

Nothing launches it for you. A host constructs and runs it, and scales by running several loops, each with its own `worker_id`, against one store.

```python
from noeta.sdk import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
print(loop.running)                      # → False
loop.run_forever(install_signals=True)   # blocks until stop()
```

Inside a `Client`, use `client.start_workers(n)` instead — see [SDK](sdk.md).

## `WorkerRuntime`

The loop drives any object with four read-only properties: `engine`, `event_log`, `content_store`, `dispatcher` (`noeta.testing.profile.RuntimeBundle` is one). Three optional methods are duck-typed:

| Method | Effect when present |
| --- | --- |
| `resolve_engine(task) -> Engine` | per-task engine; without it every task uses `rt.engine` |
| `settle_subtasks_after_step(task_id)` | drives a subtask tree the just-stepped task is waiting on |
| `take_pending_prelude(task_id)` | hands over a one-shot wake prelude the host stashed |

Each loop claims only from its own `queue`, so pools with different configurations can share one store; every task in a queue must be drivable by that queue's loops ([ADR](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md)). Cross-process work needs a real SQLite file or Postgres; `:memory:` is for tests.

## Constructor

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `rt` | `WorkerRuntime` | required | the runtime to drive |
| `worker_id` | `str` | `"noeta-worker"` | lease owner id |
| `lease_seconds` | `float` | `600.0` | initial lease per task |
| `poll_interval` | `float` | `0.5` | sleep when the queue is empty |
| `heartbeat_interval` | `float` | `30.0` | lease keepalive cadence; `<= 0` disables |
| `stale_sweep_interval` | `float` | `10.0` | `requeue_stale` cadence; `<= 0` disables |
| `timer_poll_interval` | `float` | `1.0` | `fire_due_timers` cadence; `<= 0` disables |
| `shutdown_grace_s` | `float \| None` | `30.0` | max wait for the in-flight step after `stop()`, then abandon; `None` / `<= 0` waits forever |
| `sleep`, `clock`, `now_fn`, `heartbeat_wait` | callables | `None` | time seams for tests; `now_fn` is the wall clock for timers, `clock` is monotonic |
| `reliability_sink` | `ReliabilitySink \| None` | `None` | receives `ReliabilityEvent`s; default: structured logs |
| `step_poll_s` | `float` | `0.05` | poll cadence while waiting on the step thread |
| `next_goal_handle` | `str \| None` | `None` | when set, a human stop suspends the task on this handle (reopenable) instead of ending it |
| `queue` | `str` | `"default"` | the only queue this loop claims from; match the client's `HostConfig.queue` |
| `lease_backoff_max_s` | `float` | `30.0` | cap on the doubling backoff after a dispatcher fault |

One loop is one drive thread; there is no `workers` knob. Concurrent loops are safe: writes are lease-fenced.

## Methods

| Member | Behaviour |
| --- | --- |
| `run_forever(*, install_signals=False)` | run `recover_cap_terminal()` once, then loop `maybe_sweep()` → `maybe_poll_timers()` → `tick()` until `stop()`. `install_signals=True` wires SIGTERM/SIGINT (main thread only). |
| `tick() -> bool` | lease and advance one task; `False` if the queue was empty or `lease()` faulted |
| `maybe_sweep() -> bool` | run `requeue_stale()` if due |
| `maybe_poll_timers() -> bool` | run `fire_due_timers()` if due; no-op without timer support |
| `recover_cap_terminal() -> list[str]` | startup reconciliation; returns healed task ids |
| `stop()` | stop after the current iteration |
| `running: bool` | still running |
| `abandoned: bool` | shutdown grace ran out with a step in flight — **exit the process** |

Module-level helpers:

| Function | Purpose |
| --- | --- |
| `install_stop_signals(loop) -> restore` | wire SIGTERM/SIGINT to `loop.stop()`; off the main thread warns and returns a no-op |
| `run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None, reliability_sink=None, engine=None) -> WorkerOutcome` | advance one leased task one step, including crash recovery; shared with the in-process runner |
| `keep_lease_alive(dispatcher, lease, *, interval=30.0, lease_seconds=600.0, reliability_sink=None)` | heartbeat context manager for a step driven without a loop |
| `resolve_engine(rt, task) -> Engine` | the per-task engine lookup |
| `reconcile_cap_terminal(rt, task_id) -> bool` | write the missing terminal event for one capped task (idempotent) |
| `recover_cap_terminal(rt) -> list[str]` | the same over every task |

## Errors

| Situation | What the loop does |
| --- | --- |
| `InvalidLease` | log and continue; the lease isn't ours |
| any other exception in a step | `dispatcher.fail(lease_id, retryable=True, reason=…)`; retried up to the backend's `max_fail_attempts`, then terminal |
| `fail()` itself raises | log and continue |
| dispatcher fault under `lease()` | log, emit `dispatcher_unavailable`, back off (doubling, capped at `lease_backoff_max_s`), keep polling |
| `KeyboardInterrupt` / `SystemExit` | propagate |

Provider errors never reach this: they become error `LLMResponse`s the policy handles.

**Cap-terminal reconciliation.** When a dispatcher cap (`max_fail_attempts` or `reclaim_max`) marks a row terminal, it writes nothing to the event log, so a waiting parent would never wake. The loop writes a `TaskFailed` for such tasks after `fail()`, after each sweep, and once at startup, and emits `cap_terminal_reconciled`.

## Outcomes and signals

`WorkerOutcome`:

| Value | Meaning |
| --- | --- |
| `"woken"` | the lease carried a wake; the task advanced one step |
| `"drained"` | a pending or running task advanced one step |
| `"skipped"` | suspended with no wake yet (diagnostic) |
| `"cancelled"` | a human cancel landed; task is terminal |
| `"stopped"` | a human stop landed, or crash recovery parked it; task is reopenable |

`ReliabilityEvent(kind, task_id=None, lease_id=None, detail={})` — process-local, not event-log events. Kinds: `stale_requeued`, `suspended_without_wake`, `step_failed_retryable`, `heartbeat_invalid_lease`, `shutdown_abandoned`, `timers_fired`, `attempt_abandoned` (interrupted attempt sealed and re-driven), `attempt_parked` (sealed and parked for a human), `cap_terminal_reconciled`, `dispatcher_unavailable`.

`WakeRecoveryError` — a wake can't be matched to folded state; the worker fails loudly. A crash mid-step is not an error: the next lease seals the attempt with `StepAttemptAbandoned` and re-drives it if side-effect-free, otherwise parks it.

## Shutdown

`stop()` stops leasing and waits up to `shutdown_grace_s` for the in-flight step. On timeout the loop stops the heartbeat, emits `shutdown_abandoned`, sets `abandoned` and returns without releasing the lease; the process must exit, and `requeue_stale` reclaims the task on the next start.

A heartbeat can't hold a lease forever: the dispatcher caps renewals at `heartbeat_max`, after which the step's next write fails with `InvalidLease`.

## Next

- [Deploy](../guides/deploy.md) — workers, Docker, Postgres
- [Tasks and waking](../how-it-works/tasks-and-waking.md) — how suspend and wake work
- [Limitations](../operations/limitations.md) — boundary conditions
