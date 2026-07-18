# Deploy a worker

**Goal:** run a `WorkerLoop` as a resident drain loop that continuously
processes tasks from a durable store.

**Before you start:** you understand the SDK from [Your first
agent](../tutorials/first-agent.md). You have a durable SQLite store set
up.

## What WorkerLoop is

`WorkerLoop` is the library primitive for running a resident agent that
drains the dispatcher's ready queue. It is not a console script and not
a daemon — you construct and run it in your own process:

```python
from noeta.runtime.worker import WorkerLoop

loop = WorkerLoop(rt, worker_id="my-worker")
loop.run_forever(install_signals=True)  # blocks until stop()
```

`WorkerLoop` is a dedicated drain: it polls the ready queue, leases one
task, advances it one step, and repeats. The platform embeds a pool of
them (`AGENT_NUM_WORKERS`); this page is for running your own — the
deployment shape for agents that need to outlive any individual HTTP
request.

## Build a WorkerRuntime

`WorkerLoop` expects a `WorkerRuntime` — an object with four read-only
properties:

```python
from dataclasses import dataclass

@dataclass
class MyRuntime:
    engine: ...        # noeta Engine instance
    event_log: ...     # EventLog (SQLite-backed for durability)
    content_store: ... # ContentStore
    dispatcher: ...    # Dispatcher
```

The in-repo `noeta.testing.profile.RuntimeBundle` satisfies this
protocol for testing. For a real deployment, assemble these from the
runtime's storage and engine modules.

### Use real SQLite storage

Cross-process enqueue only works through shared on-disk state. Do not
use `:memory:` for a resident worker:

```python
from noeta.storage.sqlite import (
    SqliteEventLog, SqliteContentStore, SqliteDispatcher,
)

db_path = "./worker.sqlite"
event_log = SqliteEventLog(db_path)
content_store = SqliteContentStore(db_path)
dispatcher = SqliteDispatcher(db_path)
```

All three must point to the same SQLite file (or at least the same
on-disk database) so the dispatcher can coordinate with the event log.

## Construct and run

```python
from noeta.runtime.worker import WorkerLoop, install_stop_signals

loop = WorkerLoop(
    rt,
    worker_id="noeta-worker",
    lease_seconds=600.0,
    poll_interval=0.5,
    heartbeat_interval=30.0,
    stale_sweep_interval=10.0,
    timer_poll_interval=1.0,
    shutdown_grace_s=30.0,
)

# Wire SIGTERM/SIGINT to stop() (main thread only)
install_stop_signals(loop)

# Blocks until stop() is called or a signal arrives
loop.run_forever(install_signals=True)
```

### Constructor knobs

| Knob | Default | What it does |
| --- | --- | --- |
| `worker_id` | `"noeta-worker"` | Lease owner identifier |
| `lease_seconds` | `600.0` | Initial lease deadline per task |
| `poll_interval` | `0.5` | Sleep when the ready queue is empty |
| `heartbeat_interval` | `30.0` | Per-step lease keepalive (`<= 0` disables) |
| `stale_sweep_interval` | `10.0` | `requeue_stale()` cadence (`<= 0` disables) |
| `timer_poll_interval` | `1.0` | `fire_due_timers()` poll cadence |
| `shutdown_grace_s` | `30.0` | Max wait for in-flight step after `stop()`. `None` = unbounded |
| `reliability_sink` | structured logs | Where `ReliabilityEvent`s go |

There is **no `workers` knob** — the loop is single-worker by design.

## What happens at runtime

Each iteration of `run_forever`:

1. `maybe_sweep()` — if `stale_sweep_interval` elapsed, call
   `dispatcher.requeue_stale()` to reclaim tasks whose leases expired.
2. `maybe_poll_timers()` — if `timer_poll_interval` elapsed, call
   `dispatcher.fire_due_timers()` to produce `TimerFired` wake events.
3. `tick()` — lease one ready task and advance it one step. Returns
   `False` if the queue is empty.
4. If `tick()` returned `False`, sleep `poll_interval` seconds.

## Shutting down

Call `loop.stop()` to signal a graceful shutdown. The loop:

1. Stops leasing new tasks.
2. Waits up to `shutdown_grace_s` for the in-flight step to complete
   (its lease is kept alive by the heartbeat).
3. If the step does not finish in time, **abandons** it: stops the
   heartbeat, emits `shutdown_abandoned`, sets `loop.abandoned = True`.

When `loop.abandoned` is `True`, the host **must exit the process**. The
abandoned step thread may still be writing to the EventLog; in-process
reuse after abandon is unsupported. After the process exits, the lease
expires and `requeue_stale` reclaims the task on the next start.

## Exception handling

A resident loop must not crash on a poisoned task:

- `InvalidLease` → log and continue (the lease is no longer ours).
- Any other exception → `dispatcher.fail(lease_id, retryable=True,
  reason=…)`: bounded retry, then terminal.
- If `fail()` itself raises → log and continue.

The loop always proceeds to the next task.

## See also

- [WorkerLoop reference](../reference/worker-loop.md) — every constructor
  parameter, method, and outcome type
- [Wake & resume](../concepts/wake-resume.md) — the delivery guarantee
  the worker implements
- [Known limitations](../operations/limitations.md) — single-worker
  boundaries and crash-recovery scope
