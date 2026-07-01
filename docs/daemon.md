# Resident drain (the `WorkerLoop` primitive)

> **Single-host preview.** The resident worker loop is for local and
> single-host use. It is honest about its limits: wake delivery is now
> **durable exactly-once** (single-worker; H2 / docs/adr/subtask-fanout-and-durable-wake.md), but it still
> has a **bounded process-shutdown** that abandons (does not interrupt) a
> step stuck past its shutdown grace, a bounded per-step lease-keepalive
> window, and a **single worker** with no concurrency (multi-worker is a
> separate future slice). Crash recovery is byte-equal only for the
> no-orphan-event class. Read the [Limitations](#limitations) before
> relying on it for anything that matters.

> **No shipped CLI.** TL6 removed the `noeta serve` command (and every
> other `noeta <subcommand>`); there are **no console scripts** in any
> package. The resident drain loop is now the **library primitive**
> `noeta.runtime.worker.WorkerLoop`. An embedder constructs and runs it;
> nothing in the distribution launches it for you. The chat **server**
> (the UI use-case that `noeta serve --ui` used to cover) is now the
> separate launcher `python -m noeta.agent` (see [The chat
> server](#the-chat-server)).

## What it is

A run started through the chat server (HTTP `POST /tasks`, see [The chat
server](#the-chat-server)) and a targeted resume (HTTP
`POST /tasks/{id}/resume`) are **one-shot**: they drive a task — or
re-drive one leased task — once and return. `WorkerLoop` is the
**resident** equivalent. It runs a continuous loop that:

1. leases the next ready Task from the dispatcher,
2. drives it one step (the same 3-state machine the one-shot resume uses
   — woken / drained / suspended-skip, implemented by
   `noeta.runtime.worker.run_leased_task`),
3. releases the lease, and
4. periodically reclaims stale leases left behind by crashed workers.

`WorkerLoop` lives in the L2 runtime layer
(`packages/noeta-runtime/noeta/runtime/worker.py`) so an embedding or SDK
can run the same drain loop without depending on any higher layer. It
drives any object that satisfies the narrow `WorkerRuntime` structural
Protocol — `engine` / `event_log` / `content_store` / `dispatcher`. The
in-repo `noeta.testing.profile.RuntimeBundle` (returned by
`noeta.testing.profile.build_runtime`) satisfies it, which is the easiest
way to stand one up:

```python
from noeta.runtime.worker import WorkerLoop

# rt is any WorkerRuntime: engine / event_log / content_store / dispatcher.
# noeta.testing.profile.build_runtime(...) returns a RuntimeBundle that
# satisfies it; a real embedder supplies its own wired runtime.
loop = WorkerLoop(
    rt,
    worker_id="noeta-worker",
    lease_seconds=600.0,
    poll_interval=0.5,
    heartbeat_interval=30.0,
    stale_sweep_interval=10.0,
    shutdown_grace_s=30.0,
)

# Blocks until stop() is called. install_signals=True wires SIGTERM /
# SIGINT to loop.stop() for the duration (main thread only) and restores
# the previous handlers on exit.
loop.run_forever(install_signals=True)
```

`run_forever(install_signals=True)` is the resident form. If you wire
signals yourself, use `noeta.runtime.worker.install_stop_signals(loop)`
(it returns a restore callable) and call `loop.run_forever()` without the
flag. To stop from another thread, call `loop.stop()`.

**One loop = one profile.** A `WorkerLoop` drives whatever single
`WorkerRuntime` it was constructed with, so it binds exactly one provider
/ model / tool set / policy (unless the runtime supplies a per-task
`resolve_engine(task)` seam — docs/adr/agent-identity-and-provenance.md — in which case it drives each
task with its own Agent's Engine). With the single-Engine runtime there
is no per-task provider or model resolution: every task the loop picks up
is driven with the profile the runtime was built with.

This has a sharp consequence: **every task in a given store must be
compatible with the loop that drains it.** The dispatcher hands out one
shared ready queue with no task routing, so a loop will lease and drive
*any* ready task — including one enqueued for a different intended
profile. To run different profiles, give each its **own sqlite file** (or
partition work with an external queue); do not point two differently
configured loops at the same store. Same-store routing / a per-task
profile resolver is future work, as is multi-worker concurrency (see
[Limitations](#limitations)).

## Storage: a real file for cross-process enqueue

A resident drain loop exists to host tasks enqueued by *other* processes
(the SDK, an operator script, the chat server elsewhere), and
cross-process enqueue only works through shared on-disk storage. Build
the loop's runtime over a **real sqlite file** so a task enqueued against
`./state.sqlite` from any process is picked up by a running loop on the
same file — no restart needed.

`:memory:` is accepted but is **dev/test-only**: an in-memory stack is
private to the process that created it, so nothing else can ever enqueue
into it. Use it only for smoke tests of the loop itself.

## Knobs

`WorkerLoop`'s behavior is set by its constructor arguments (there is no
flag surface — it is a library object). The relevant ones:

| Constructor arg | Default | Meaning |
| --- | --- | --- |
| `rt` | *(required)* | The `WorkerRuntime` to drive (binds the single profile). |
| `worker_id` | `"noeta-worker"` | Lease owner id. |
| `lease_seconds` | `600.0` | Initial lease deadline granted per task. |
| `poll_interval` | `0.5` | Seconds to sleep when the ready queue is empty. |
| `heartbeat_interval` | `30.0` | How often the per-step heartbeat extends a slow step's lease (`<= 0` disables the heartbeat). |
| `stale_sweep_interval` | `10.0` | Interval between `requeue_stale` sweeps (`<= 0` disables sweeping). |
| `shutdown_grace_s` | `30.0` | On stop, max seconds to wait for the in-flight step before **abandoning** it (H1). `None` / `<= 0` = the old unbounded wait. |
| `reliability_sink` | structured logs | Where process-local `ReliabilityEvent`s go. |

There is **no `workers` knob.** The loop is single-worker by design in
this preview (see [Limitations](#limitations)).

## The chat server

The UI use-case that `noeta serve --ui` used to cover is now the separate
launcher **`python -m noeta.agent`** (sources:
`apps/noeta-agent/noeta/agent/__main__.py` and `host/runner_cli.py`). It is **not
an argparse CLI** and takes **zero positional args**: it reads config
from env (or a `NOETA_AGENT_CONFIG` JSON file via
`noeta.agent.host.runner_cli.RunnerConfig.from_env`), boots an HTTP/SSE chat server
plus the bundled web SPA, prints the served URL, and blocks until SIGINT
/ SIGTERM. It always serves the UI — there are **no `--ui` / `--serve`
flags** (those were `noeta serve` flags and are gone).

```bash
# the env-configured launcher — equivalent of the old "noeta serve --ui"
NOETA_AGENT_PROVIDER=stub \
NOETA_AGENT_SQLITE_PATH=./state.sqlite \
python -m noeta.agent
```

Config is read entirely from env (defaults in parens): `NOETA_AGENT_PROVIDER`
(`stub`), `NOETA_AGENT_SQLITE_PATH` (`:memory:`), `NOETA_AGENT_PORT` (`0` =
ephemeral), `NOETA_AGENT_HOST` (`127.0.0.1`), `NOETA_AGENT_MODEL`
(`stub-model`), `NOETA_AGENT_WORKSPACE` (cwd), optional
`NOETA_AGENT_API_KEY` / `NOETA_AGENT_BASE_URL`, or `NOETA_AGENT_CONFIG` pointing
at a JSON file with the same keys.

The first stdout line is the served URL (`noeta.agent serving at
http://127.0.0.1:<port>/`). Open it to watch the queue and to re-drive
individual tasks with the Resume surface. Note this launcher
serves the UI and the HTTP command surface; it starts **no** `WorkerLoop`
(delegation is off, so chat tasks only ever suspend on a human handle the
inline driver resolves synchronously — there is nothing to drain). If you
need the resident drain loop, construct a `WorkerLoop` as shown above —
they are separate concerns.

## Lifecycle

* **Continuous drain** — each iteration leases one ready task and runs a
  single step (`WorkerLoop.tick()`). When the queue is empty the loop
  sleeps `poll_interval` seconds, then tries again.
* **Periodic stale-sweep** — every `stale_sweep_interval` seconds the loop
  runs `requeue_stale()` (`WorkerLoop.maybe_sweep()`), returning leases
  whose deadline passed (e.g. a crashed worker) to the ready queue.
* **Per-step heartbeat** — while a single step runs, a side thread extends
  that step's lease every `heartbeat_interval` seconds, so a legitimately
  slow step is not reclaimed mid-flight.
* **Worker exception policy** — a resident loop must not crash on one
  poisoned task. If a step raises, the loop fails the lease as retryable
  (bounded retry, then terminal) and moves on; if the lease was already
  lost (`InvalidLease`), it logs and continues without claiming anything
  about the task's state.

## Shutdown

`loop.stop()` (which `install_signals=True` wires to SIGTERM / SIGINT)
triggers **bounded process-shutdown** (H1): the loop stops leasing new
tasks and waits up to `shutdown_grace_s` seconds for the in-flight step to
finish (its lease kept alive by the heartbeat). If it finishes in time it
releases normally and the loop returns. If the grace elapses the loop
**abandons** the step — stops its heartbeat (so the lease will expire),
emits a `shutdown_abandoned` reliability event, sets
`WorkerLoop.abandoned`, and returns without touching the lease.

```python
# the resident equivalent of Ctrl+C / kill -TERM: another thread, a
# signal handler, or install_signals=True flips the running flag.
loop.stop()
```

**Process-shutdown only — not a safe in-embedding continue.** Python
cannot interrupt the abandoned step thread; it may still be running and
may still write the EventLog. So abandon is only safe because the
**process exits**: the abandoned thread dies with it, the lease then
expires, and `requeue_stale` reclaims the task on the next start. Reusing
the same runtime/loop in-process after `WorkerLoop.abandoned` is set is
**unsupported** — the host MUST exit the process. `shutdown_grace_s=None`
/ `<= 0` restores the old unbounded wait. See [Limitations](#limitations).

## Limitations

These are deliberate boundaries of the single-host preview, not bugs.

### Durable exactly-once wake (H2)

A suspended task's wake is delivered and consumed **exactly once, even
across a crash** (docs/adr/subtask-fanout-and-durable-wake.md). The matched wake **survives the `lease()`**
(it is no longer destroyed at lease time); it is cleared only by a
**consuming release** that presents the wake it consumed (after the
durable `TaskWoken` is written), and is otherwise **re-delivered** by
`requeue_stale` after a crash. The worker's woken branch is a recovery
state machine keyed on the latest matching `TaskWoken` within the current
suspend-window, so a re-delivery after a crash that already wrote
`TaskWoken` is reconciled (terminal / re-suspended / continue) **without a
second `TaskWoken`**.

Net: *the wake that should fire always fires; a re-delivered wake is
consumed only once.* No operator re-issue is needed. This is **single-host
/ single-worker** exactly-once — multi-worker concurrency (and the
completion-ordering / fencing it implies) is a future slice. A step that
crashes **mid-flight** (after `TaskWoken`, partial step events, still
running) remains the documented **partial-step-orphan** limitation below —
H2 does not silently re-run a partial step.

### Shutdown — bounded, but still no in-process interrupt

On `stop()` (SIGTERM / SIGINT, or a direct call) the loop stops leasing
and waits up to `shutdown_grace_s` seconds for the in-flight step, then
**abandons** it and returns (H1). It still does **not** interrupt a
running step (Python cannot kill the thread) — abandon is
**process-shutdown**: the host MUST exit, the abandoned thread dies with
the process, its lease expires, and `requeue_stale` reclaims the task on
the next start. A stuck step therefore no longer holds the loop forever,
but the in-flight attempt is **not** cleanly finished. See
[`failure-modes.md`](failure-modes.md).

### Crash recovery is scoped to the no-orphan-event class

A worker that dies **before writing any durable step event** (lease-only
/ pre-`TaskStarted`) is fully recoverable: `requeue_stale` returns the
task and a fresh worker rebuilds it via `fold` and drives it to
completion — the recording is byte-equal to a no-crash run. A
**partial-step crash that leaves orphan events** (the process dies after
`ContextPlanComposed` / `LLMRequestStarted` / `ToolCallStarted` but
before the paired completion event) is a **known limitation**: `fold`
may rebuild state, but a from-scratch replay does not reproduce the
orphan attempt. Closing this needs an attempt-journal / replay-semantics
mechanism (its own ADR) — it is **not** solved here.

### Reliability events are process-local (not the EventLog)

The worker emits process-local `ReliabilityEvent`s — `stale_requeued`,
`suspended_without_wake`, `step_failed_retryable`,
`heartbeat_invalid_lease`, `shutdown_abandoned` — to an injectable sink
(`reliability_sink`; default: structured logs). These are **not** EventLog
events, are **not** persisted or replayed, and each is named for what the
worker can prove from the dispatcher seam (e.g. `heartbeat_invalid_lease`
is a symptom — the cause may be cap / expired / requeued / released;
`step_failed_retryable` means the worker called `fail(retryable=True)`,
not that the task went terminal).

### Heartbeat keepalive window

The heartbeat keeps a slow step's lease alive, but not forever. The
dispatcher caps the number of heartbeat extensions (`heartbeat_max`), so
`heartbeat_interval × heartbeat_max` is the maximum time one step can hold
a lease. Past that cap the lease is force-released and the step's next
EventLog write fails with `InvalidLease`. **This cap-hit is an
operational-failure path, not a recovery path** — 3A adds no automatic
cap-hit recovery; it may need operator inspection.

### Single worker

The loop runs one worker. There is no in-process concurrency and no
`workers` knob. Throughput is one step at a time. Multi-worker /
multi-host coordination is out of scope for this preview.

## See also

* [`failure-modes.md`](failure-modes.md) — recovery recipes for the
  limitations above.
* [`noeta-agent.md`](noeta-agent.md) — the `python -m noeta.agent` coding
  agent and its HTTP surface (the local chat/trace web UI).
* [`concepts.md`](concepts.md) — the lease / dispatcher / Task model the
  drain loop sits on top of.

> **Trace export** is a library observer, not a shell command:
> `noeta.observers.trace_export.make_jsonl_trace_observer(
> event_log=..., path=...)`, wired by embedders (e.g.
> `noeta.testing.profile.build_runtime(trace_file=...)`). There is no
> verify/replay command, HTTP endpoint, or `noeta.verify` API: re-deriving
> a task's state is just `fold` over its EventLog (no provider re-call),
> and re-driving one is the targeted resume above (`POST /tasks/{id}/resume`
> over `noeta.runtime.worker.run_leased_task`).
