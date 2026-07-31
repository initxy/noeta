# Deploy a worker

This guide shows you how to run a resident pool of workers that continuously
drains a durable store, so tasks keep progressing after the request that created
them is gone. You need the SDK basics from
[Your first agent](../tutorials/first-agent.md).

## Why you need one

A task advances only when something leases it off the dispatcher's ready
queue. Nothing launches that drain for you. Without a running worker:

- a `wait_timer` suspension never wakes — the worker's timer poll is the only
  producer of `TimerFired`;
- a task whose worker crashed mid-step is never reclaimed, because the
  stale-lease sweep runs inside the drain loop;
- a task made ready by one process is never picked up by another.

A worker is the deployment shape for anything that must outlive the request
that created it.

## 1. Use durable storage

Cross-process handoff only works through shared on-disk state, so a resident
pool needs real storage. `HostConfig.storage_path` takes one string — a SQLite
file path, a `postgresql://` DSN, or `":memory:"` — and builds the whole
`(EventLog, ContentStore, Dispatcher)` triple in the right order:

```python
from pathlib import Path

from noeta.sdk import Client, HostConfig, Options

options = Options(system_prompt="You are a helpful assistant.", name="main")

client = Client(
    options,
    provider=my_provider,
    workspace_dir=Path("./workspace"),
    model="claude-sonnet-4-5-20250929",
    host_config=HostConfig(storage_path="./noeta.sqlite"),
)
```

Do not use `":memory:"` for a resident pool — the store dies with the process
and nothing else can see it.

If you build the triple yourself, use `noeta.sdk.storage` and keep all three
components on the same database; the event log takes the dispatcher as its
lease validator:

```python
from noeta.sdk.storage import build_storage_stack

event_log, content_store, dispatcher = build_storage_stack(
    "sqlite", path="./noeta.sqlite",
)
```

`build_storage_stack` accepts `"memory"` (no config), `"sqlite"` (`path=`) and
`"postgres"` (`dsn=`). `open_storage_stack` runs the same value-shape dispatch
`HostConfig.storage_path` uses. Pass the triple as the `event_log` /
`content_store` / `dispatcher` fields of `HostConfig` — all three or none;
mixing them with `storage_path` raises `ValueError`.

## 2. Start and stop the pool

```python
with client:
    client.start_workers(4)
    ...
    stopped = client.stop_workers(timeout=30.0)
    print("all workers exited:", stopped)
```

```
all workers exited: True
```

Each worker runs on its own daemon thread with its own `worker_id`, and all of
them drain the same ready queue. Concurrent workers are safe: every
lease-checked append is fenced, so a worker whose lease was reclaimed is
rejected rather than allowed to write.

`start_workers` is one-shot — a second call raises `RuntimeError`.
`stop_workers` signals every loop and joins the threads, returning `True` when
all of them exited within `timeout`. On a timeout it returns `False` and
deliberately keeps the pool tracked, so a retry can finish the job instead of
stacking a second pool on the first. `Client.shutdown` (and therefore leaving
the `with` block) stops the pool before tearing anything else down.

## 3. Tune the knobs

| Parameter | Default | What it does |
| --- | --- | --- |
| `num_workers` | `1` | Number of drain threads. Must be `>= 1`. |
| `poll_interval` | `0.1` | Sleep when the ready queue is empty |
| `heartbeat_interval` | `30.0` | Per-step lease keepalive |
| `stale_sweep_interval` | `10.0` | `requeue_stale()` cadence |
| `timer_poll_interval` | `1.0` | `fire_due_timers()` cadence |
| `lease_seconds` | `600.0` | Initial lease deadline per task |
| `shutdown_grace_s` | `10.0` | Max wait for an in-flight step after stop. `None` = unbounded |

## What a worker does per iteration

1. Sweep stale leases — `requeue_stale()` reclaims tasks whose leases expired.
2. Poll timers — `fire_due_timers()` flips every due `wait_timer` suspension
   back to ready with a `TimerFired` wake.
3. Lease one ready task and advance it one step.
4. If the queue was empty, sleep `poll_interval`.

A poisoned task never crashes the loop. An `InvalidLease` is logged and
skipped — the lease is not this worker's, so it makes no claim about the
task. Any other exception becomes `dispatcher.fail(retryable=True)`: bounded
retry, then terminal. The loop always proceeds to the next task.

## Shutdown

A stop request is cooperative. The loop stops leasing new tasks and waits up
to `shutdown_grace_s` for the in-flight step, whose lease the heartbeat keeps
alive. If the step does not finish in time the loop **abandons** it and
returns.

Abandon is process-shutdown only. Python cannot kill the abandoned step
thread, so it may still be running and writing to the EventLog: the host must
exit the process. Once it does, the lease expires and the next
`requeue_stale` sweep reclaims the task.

## Scaling out

Several processes may drain one store only on **Postgres**, where appends are
fenced in-transaction against the live lease and lease expiry runs on the
database clock. SQLite has no cross-host fencing — keep it to one host, where
a multi-worker pool is fine.

The ready queue does no routing: a worker drains whatever it leases, so every
task in a store must be one that pool can run. Give distinct workload profiles
their own store.

## Driving the loop yourself

A host that has no `Client` — a standalone drain process assembling its own
engine, event log, content store and dispatcher — can construct the drain
primitive directly. See the [WorkerLoop
reference](../reference/worker-loop.md) for the `WorkerRuntime` protocol it
expects and every constructor parameter, method, and outcome type.

## Next steps

- [WorkerLoop reference](../reference/worker-loop.md) — the loop primitive in
  full
- [Deploy with Docker](docker-deployment.md) — packaging the pool as an image
- [Wake & resume](../concepts/wake-resume.md) — the delivery guarantee the
  worker implements
- [Known limitations](../operations/limitations.md) — the SQLite single-host
  boundary and crash-recovery scope
