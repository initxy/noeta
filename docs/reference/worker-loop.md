# WorkerLoop

A worker is the thing that picks up a waiting task and pushes it one step
forward. `WorkerLoop` is that loop, shipped as a library primitive: it leases a
ready task, advances it, releases, and repeats — plus heartbeats, stale-lease
sweeps, timer polling and a bounded graceful shutdown.

There is no console script and nothing launches it for you. An embedding host
constructs and runs it, and scales by running several loops — each with its own
`worker_id` — against one store.

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
print(loop.running)                      # → False
loop.run_forever(install_signals=True)   # blocks until stop()
```

If you only need workers inside an existing `Client`, call
`client.start_workers(n)` instead — see [query / Client](sdk-client.md).

Members below are named, not line-numbered: line numbers drift on every edit, so
the module path plus the member name is the stable coordinate.

## `WorkerRuntime` protocol

The loop drives any object exposing four read-only properties: `engine`,
`event_log`, `content_store`, `dispatcher`. The in-repo
`noeta.testing.profile.RuntimeBundle` satisfies it. Three further methods are
**duck-typed** — a runtime that omits them degrades to a no-op:

| Method | Effect when present |
| --- | --- |
| `resolve_engine(task) → Engine` | the per-task engine resolver a multi-agent host supplies; without it the loop always uses the single `rt.engine`, so one loop binds one provider / model / tool set / policy |
| `settle_subtasks_after_step(task_id)` | drives a delegation subtree the just-driven task barriered on (the resident path has no in-request drain to seed its children) |
| `take_pending_prelude(task_id)` | hands over a one-shot non-durable woken prelude the host stashed at seed-yield time |

Tasks in a store must be compatible with the loop that drains it (the ready
queue has no routing): give different profiles their own sqlite files.

Use a **real sqlite file** for the runtime's storage — cross-process enqueue
only works through shared on-disk state; `:memory:` is dev/test-only.

## Constructor

```python
WorkerLoop(
    rt: WorkerRuntime,
    *,
    worker_id: str = "noeta-worker",
    lease_seconds: float = 600.0,
    poll_interval: float = 0.5,
    heartbeat_interval: float = 30.0,
    stale_sweep_interval: float = 10.0,
    timer_poll_interval: float = 1.0,
    shutdown_grace_s: Optional[float] = DEFAULT_SHUTDOWN_GRACE_S,   # 30.0
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    now_fn: Optional[Callable[[], float]] = None,
    heartbeat_wait: Optional[Callable[[float], bool]] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
    step_poll_s: float = 0.05,
    next_goal_handle: Optional[str] = None,
)
```

| Knob | Meaning |
| --- | --- |
| `worker_id` | lease owner id |
| `lease_seconds` | initial lease deadline granted per task |
| `poll_interval` | sleep when the ready queue is empty |
| `heartbeat_interval` | per-step lease keepalive cadence (`<= 0` disables) |
| `stale_sweep_interval` | cadence of `requeue_stale` sweeps (`<= 0` disables) |
| `timer_poll_interval` | cadence of the `fire_due_timers` poll (the `TimerFired` producer; `<= 0` disables) |
| `shutdown_grace_s` | max wait for an in-flight step after `stop()`, then **abandon**; `None` / `<= 0` = unbounded wait |
| `sleep` / `clock` / `now_fn` / `heartbeat_wait` | injectable time seams (tests); `now_fn` is the **wall** clock the timer due-check uses, kept separate from the monotonic `clock` |
| `reliability_sink` | where `ReliabilityEvent`s go; default: structured logs |
| `step_poll_s` | poll cadence while waiting on the in-flight step thread |
| `next_goal_handle` | when set, a human close / cancel suspends the task on this handle (reopenable by typing again) instead of releasing it terminal |

There is **no `workers` knob**: one `WorkerLoop` is one drain thread. You scale
by running several loops (each with its own `worker_id`) against the same
store. Concurrent loops are safe: lease-checked appends are fenced, so a loop
whose lease was reclaimed cannot write behind the loop that took over.

## Methods and properties

| Member | Behavior |
| --- | --- |
| `run_forever(*, install_signals=False)` | drive until `stop()`; each iteration: `maybe_sweep()` → `maybe_poll_timers()` → `tick()`, sleeping `poll_interval` when idle. `install_signals=True` wires SIGTERM/SIGINT to `stop()` (main thread only) and restores handlers on exit |
| `tick() → bool` | lease one ready task and advance it one step; `False` when the queue is empty. The exception policy is applied inside |
| `maybe_sweep() → bool` | run `requeue_stale()` if the interval elapsed |
| `maybe_poll_timers() → bool` | run `fire_due_timers()` if the interval elapsed; degrades to a no-op on a dispatcher without timers |
| `stop()` | signal the loop to stop after the current iteration |
| `running: bool` | loop still running |
| `abandoned: bool` | set when the shutdown grace elapsed with a step still in flight. The host **must exit the process** — the abandoned step thread may still write the EventLog; in-process reuse is unsupported |

Module-level helpers:

- `install_stop_signals(loop) → restore()` — wire SIGTERM/SIGINT to
  `loop.stop()`; off the main thread it warns and returns a no-op restore.
- `run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None,
  reliability_sink=None, engine=None) → WorkerOutcome` — the canonical 3-state
  resume machine (including crash-recovery seal / re-drive / park), shared with
  the in-process runner so the two cannot drift.
- `keep_lease_alive(dispatcher, lease, *, interval=30.0, lease_seconds=600.0,
  reliability_sink=None)` — the per-step heartbeat context manager, for callers
  that drive a leased step with no resident loop around it.
- `resolve_engine(rt, task) → Engine` — the seam behind the per-task resolver.

## Exception policy

A resident loop must not crash on a poisoned task:

- `InvalidLease` → log + continue; no `release` / `fail` (the lease is not
  ours).
- Any other exception → `dispatcher.fail(lease_id, retryable=True,
  reason=…)`: bounded retry up to the backend's `max_fail_attempts`, then
  terminal.
- If `fail()` itself raises → log + continue.
- The loop always proceeds to the next task.

Provider failures never reach this backstop: `runtime/llm.py` translates a
provider exception into an error `LLMResponse` the policy reads, so retries are
consumed there rather than double-counted here.

## Outcome and reliability types

`WorkerOutcome`: `"woken" | "drained" | "skipped" | "cancelled" | "stopped"`.
`"skipped"` means a suspended task with no wake yet (a diagnostic, not an
error); `"cancelled"` / `"stopped"` mean a human cancel/close landed mid-turn —
`"cancelled"` left the task terminal, `"stopped"` left it reopenable.
`"stopped"` also covers a crash-recovery **park**: the task rests suspended with
a system notice, and typing a message resumes it.

`ReliabilityEvent` — process-local signals (**not** EventLog events), sent to
`reliability_sink`. Kinds: `stale_requeued`, `suspended_without_wake`,
`step_failed_retryable`, `heartbeat_invalid_lease`, `shutdown_abandoned`,
`timers_fired`, `attempt_abandoned`, `attempt_parked` (the last two are the
crash-recovery moments: an interrupted attempt sealed and re-driven
automatically, or sealed and parked for a human). Every kind names what the loop
can actually prove from the Dispatcher seam, never a root cause it cannot
observe.

`WakeRecoveryError` — a woken lease's wake cannot be reconciled against folded
state; the worker fails loud. A crash mid-step is **not** an error path: on the
next lease the interrupted attempt is sealed with `StepAttemptAbandoned` and
re-driven automatically when it is side-effect-free per the approval surface, or
the task is parked for a human (see
[known limitations](../operations/limitations.md)).

## Shutdown semantics

`stop()` stops leasing and waits up to `shutdown_grace_s` for the in-flight
step (its lease kept alive by the heartbeat). On timeout the loop
**abandons** the step: stops its heartbeat, emits `shutdown_abandoned`, sets
`abandoned`, and returns without releasing or failing the lease. Python cannot
interrupt the step thread — abandon is only safe because the process exits; the
lease then expires and `requeue_stale` reclaims the task on the next start.

The heartbeat cannot extend a lease forever: the dispatcher caps extensions at
`heartbeat_max`, so `heartbeat_interval × heartbeat_max` bounds one step's hold;
past the cap the lease is force-released and the step's next write fails with
`InvalidLease`. Boundary conditions — the SQLite single-host limit,
crash-recovery scope — are catalogued in
[known limitations](../operations/limitations.md).

## Next

- [Deploy a worker](../how-to/deploy-worker.md) — the task-oriented guide
- [Wake & resume](../concepts/wake-resume.md) — the durable, single-worker,
  exactly-once delivery guarantee
- [query / Client](sdk-client.md) — the in-process pool alternative
- [Known limitations](../operations/limitations.md) — the boundary conditions
