# WorkerLoop reference

The resident drain loop, shipped as the library primitive
`noeta.runtime.worker.WorkerLoop`
(`packages/noeta-runtime/noeta/runtime/worker.py:752`). There is no console
script and nothing launches it for you — an embedder constructs and runs it.
Note that `python -m noeta.agent` does **not** start a `WorkerLoop`; the chat
server drives turns inline (see the [coding-agent manual](noeta-agent.md)).

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="noeta-worker")
loop.run_forever(install_signals=True)   # blocks until stop()
```

## `WorkerRuntime` protocol — `worker.py:205`

The loop drives any object exposing four read-only properties: `engine`,
`event_log`, `content_store`, `dispatcher`. The in-repo
`noeta.testing.profile.RuntimeBundle` satisfies it. A multi-agent host may
additionally provide `resolve_engine(task) → Engine` — the per-task resolver
seam (`worker.py:237`); without it the loop always uses the single
`rt.engine`, so one loop binds one provider / model / tool set / policy.
Tasks in a store must be compatible with the loop that drains it (the ready
queue has no routing): give different profiles their own sqlite files.

Use a **real sqlite file** for the runtime's storage — cross-process enqueue
only works through shared on-disk state; `:memory:` is dev/test-only.

## Constructor — `worker.py:775-792`

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
    shutdown_grace_s: Optional[float] = 30.0,   # DEFAULT_SHUTDOWN_GRACE_S (worker.py:79)
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    now_fn: Optional[Callable[[], float]] = None,
    heartbeat_wait: Optional[Callable[[float], bool]] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
    step_poll_s: float = 0.05,
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

There is **no `workers` knob** — the loop is single-worker by design.

## Methods & properties

| Member | Behavior | Source |
| --- | --- | --- |
| `run_forever(*, install_signals=False)` | drive until `stop()`; each iteration: `maybe_sweep()` → `maybe_poll_timers()` → `tick()`, sleeping `poll_interval` when idle. `install_signals=True` wires SIGTERM/SIGINT to `stop()` (main thread only) and restores handlers on exit | `worker.py:1093` |
| `tick() → bool` | lease one ready task and advance it one step; `False` when the queue is empty. The exception policy is applied inside | `worker.py:864` |
| `maybe_sweep() → bool` | run `requeue_stale()` if the interval elapsed | `worker.py:882` |
| `maybe_poll_timers() → bool` | run `fire_due_timers()` if the interval elapsed; degrades to a no-op on a dispatcher without timers | `worker.py:906` |
| `stop()` | signal the loop to stop after the current iteration | `worker.py:841` |
| `running: bool` | loop still running | `worker.py:846` |
| `abandoned: bool` | set when the shutdown grace elapsed with a step still in flight. The host **must exit the process** — the abandoned step thread may still write the EventLog; in-process reuse is unsupported | `worker.py:850` |

Helpers: `install_stop_signals(loop) → restore()` (`worker.py:1118`);
`run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None) →
WorkerOutcome` — the canonical 3-state resume machine shared with the
in-process runner (`worker.py:390`); `keep_lease_alive(...)` — the per-step
heartbeat context manager (`worker.py:708`).

## Exception policy — `worker.py:755-767`

A resident loop must not crash on a poisoned task:

- `InvalidLease` → log + continue; no `release` / `fail` (the lease is no
  longer ours).
- Any other exception → `dispatcher.fail(lease_id, retryable=True,
  reason=…)`: bounded retry, then terminal.
- If `fail()` itself raises → log + continue.
- The loop always proceeds to the next task.

## Outcome and reliability types

`WorkerOutcome` (`worker.py:148`):
`"woken" | "drained" | "skipped" | "cancelled" | "stopped"` — `"skipped"`
means a suspended task with no wake yet (a diagnostic, not an error);
`"cancelled"` / `"stopped"` mean a human cancel/close landed mid-turn.

`ReliabilityEvent` (`worker.py:120`) — process-local signals (**not**
EventLog events), sent to `reliability_sink`. Kinds (`worker.py:110`):
`stale_requeued`, `suspended_without_wake`, `step_failed_retryable`,
`heartbeat_invalid_lease`, `shutdown_abandoned`, `timers_fired`.

Exceptions: `WakeRecoveryError` (`worker.py:153`) — a woken lease's wake
cannot be reconciled against folded state; the worker fails loud.
`PartialStepOrphan` (`worker.py:160`) — after a durable `TaskWoken`, a step
crashed mid-flight; the worker does not silently re-run the partial step.

## Shutdown semantics

`stop()` stops leasing and waits up to `shutdown_grace_s` for the in-flight
step (its lease kept alive by the heartbeat). On timeout the loop
**abandons** the step: stops its heartbeat, emits `shutdown_abandoned`, sets
`abandoned`, and returns. Python cannot interrupt the step thread — abandon
is only safe because the process exits; the lease then expires and
`requeue_stale` reclaims the task on the next start.

The heartbeat cannot extend a lease forever: the dispatcher caps extensions,
so `heartbeat_interval × heartbeat_max` bounds one step's hold; past the cap
the lease is force-released and the step's next write fails with
`InvalidLease`. Boundary conditions — single worker, the partial-step-orphan
edge, crash-recovery scope — are catalogued in
[known limitations](../operations/limitations.md).

## See also

- [Wake & resume](../concepts/wake-resume.md) — the delivery guarantee
- [Architecture overview](../architecture/overview.md) — the wake machinery
- [How-to: deploy a worker](../how-to/deploy-worker.md)
