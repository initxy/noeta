# Known limitations

Boundaries of what the shipped code can do. Every entry says what the boundary
is, when you hit it, and the workaround if there is one.

**None of these is a bug.** They are places where the design deliberately stops —
usually because going further would require the library to own something a host
should own. If you are chasing a fault instead, start at
[troubleshooting](troubleshooting.md).

Six groups:
[what the library will not run](#what-the-library-will-not-run-for-you) ·
[durability](#durability-boundaries) ·
[observability](#observability-gaps) ·
[growth and cost](#growth-and-cost) ·
[sandbox](#sandbox-boundaries) ·
[closed extension points](#closed-extension-points).

## What the library will not run for you

### The libraries run no process for you

**What it means:** `noeta-runtime` and `noeta-sdk` are libraries. There is no
CLI, no console script, no HTTP or SSE server, and no scheduler daemon. The
drain loop ships as the primitive `noeta.runtime.worker.WorkerLoop`; a host
constructs and runs it (or calls `Client.start_workers(n)` for a resident
pool). Nothing launches it for you, so a task enqueued with no worker running
simply sits in the ready queue.

**When you hit it:** You expected `noeta run` to exist, or you enqueued work
and nothing advanced.

**Workaround:** Embed the libraries in your own host.
`examples/reference-host` is the smallest one, assembled from the public
surface alone.

## Durability boundaries

### Multi-host coordination requires Postgres

**What it means:** Single-host multi-worker is supported — one process runs a
resident `WorkerLoop` pool and several tasks' turns progress at once. Multiple
*host processes* sharing one database is supported only on **Postgres**: event
appends are fenced in-transaction against the live lease, lease expiry is
computed on the database clock so per-host clock skew cannot split-brain, and a
`worker_id` column records the holder. The **SQLite** and **in-memory**
backends are single-host — they have no cross-host fencing, so pointing two
host processes at one SQLite file is unsafe.

**When you hit it:** You want worker processes on more than one machine
draining a shared store.

**Workaround:** Use the Postgres backend for multi-host deployments. On SQLite,
keep to a single host — a multi-worker pool on that host is fine, and
**within one process** any number of differently-configured clients may share
one storage triple: each names its own `queue` (`HostConfig.queue`), roots are
born on their seeding client's queue, children inherit it, and an untargeted
worker poll never crosses queues — so clients cannot drive each other's work.
The single-host limit is about *processes*, not clients.

See [ADR: Multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)
and [ADR: Worker queue routing](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md).

### Crash recovery does not undo side effects

**What it means:** A worker crash mid-step (`kill -KILL`, power loss) is
recovered on the next lease: the interrupted attempt is sealed with a durable
`StepAttemptAbandoned` marker, and the step is re-driven when everything the
attempt recorded would have run without a human approval gate. When the attempt
had unprovable side effects — or after three consecutive seals in one turn —
the task is **parked** instead: suspended as a stopped conversation with an
`origin="system"` notice naming each interrupted call and whether it completed.
A crash during a human-approved tool execution always parks, re-suspended on
the same approval. Recovery never silently terminates a task and never silently
re-runs a side-effectful call — but it also cannot undo anything the crashed
attempt already did.

**When you hit it:** A hard kill lands during an attempt that had already run
side-effectful tools. Normal SIGTERM shutdown does not trigger this, and a
crash during reads or planning recovers with nobody involved.

**Workaround:** Open the parked conversation — the notice lists what was
interrupted. Verify whether those operations applied fully, partially, or not
at all, then type to continue (the turn resumes from the clean pre-attempt
baseline) or re-approve the pending call.

### Shutdown can abandon a step that keeps running

**What it means:** On `stop()`, `WorkerLoop` waits up to `shutdown_grace_s` for
the in-flight step to complete. If it does not finish, the loop **abandons** the
step and returns — but Python cannot kill the abandoned thread. It may still be
running and writing to the EventLog.

**When you hit it:** A step hangs (a tool call to an unresponsive external API,
say) and the grace window expires.

**Workaround:** **Exit the process.** After abandon, the host must call
`sys.exit()` or equivalent. The abandoned thread dies with the process, its
lease expires, and `requeue_stale()` reclaims the task on the next start.
`shutdown_grace_s=None` (or `<= 0`) waits unboundedly — then a stuck step needs
an external `kill -KILL <pid>`.

### The heartbeat keepalive window is capped

**What it means:** The heartbeat keeps a slow step's lease alive, but not
forever. The dispatcher caps heartbeat extensions at `heartbeat_max` (360 by
default), so `heartbeat_interval × heartbeat_max` is the maximum time one step
can hold a lease. Past the cap the lease is force-released and the step's next
EventLog write fails with `InvalidLease`.

**When you hit it:** A single step — one model turn plus all its tool calls —
takes longer than the cap window. With the defaults that is hours, so it is
rare.

**Workaround:** Treat a cap hit as an operational-failure signal, not a
recovery path. The loop logs it and continues to the next task, but the capped
task may need inspection: check whether it is still viable or should be closed.

## Observability gaps

### Reliability events are process-local

**What it means:** The worker emits `ReliabilityEvent`s — `stale_requeued`,
`suspended_without_wake`, `step_failed_retryable`, `heartbeat_invalid_lease`,
`shutdown_abandoned`, `timers_fired`, `attempt_abandoned`, `attempt_parked` —
to an injectable sink that defaults to structured logs. They are **not**
EventLog events, are not persisted, and do not survive a restart.

**When you hit it:** You are building monitoring or alerting on worker
reliability signals.

**Workaround:** Mount a custom `reliability_sink` that forwards them to your
monitoring system. Each event is named for what the worker can prove from the
dispatcher seam — `heartbeat_invalid_lease`, for instance, is a symptom whose
cause may be the cap, expiry, or a requeue.

### Nothing notifies anyone that a task is waiting on a human

**What it means:** Human-in-the-loop is fully wired in-band: the engine
suspends on a `HumanResponseReceived` wake condition and the `answer` client
verb delivers the response. There is no out-of-band channel — no webhook, no
email, no cross-task inbox fires when a task starts waiting.

**When you hit it:** An agent asks a question while nobody is driving the task.
The task waits durably, which is the point, but nothing tells anyone.

**Workaround:** Drive the task interactively, or subscribe an `Observer` to the
EventLog, forward `UserQuestionRequested` events to your own notification
channel, and deliver the reply with `answer`.

## Growth and cost

### An uncatalogued model silently disables compaction and pricing

**What it means:** Compaction knobs and cost are both derived from the model
catalog in the `providers` built-in. For a model the catalog does not describe,
`derive_compaction_config` returns `COMPACTION_OFF` — context compaction never
engages, so a long conversation runs until the provider itself rejects the
request. Pricing degrades the same way: an unpriced model costs `0.0` per
round-trip, so `GovernanceState.cost` stays zero and a `max_cost_usd` budget
can never fire. Neither degradation raises.

**When you hit it:** You point `Options.model` at a gateway model id, a
fine-tune, or a self-hosted model that is not in `CATALOG`.

**Workaround:** Add a `ModelSpec` row for it. `CATALOG` and `ModelSpec` are
re-exported from `noeta.sdk.providers`; a row supplies `context_window`,
`max_output_tokens`, and the price fields, which is everything both derivations
read.

### Content is never garbage-collected

**What it means:** The ContentStore is content-addressed and append-only, and
no GC ships. `Client.delete_task` purges a task's event stream and dispatcher
state across the whole subtask tree, but deliberately leaves the content blobs
alone — they are shared by hash across tasks, so deleting one task cannot prove
a body is unreachable. Storage therefore grows monotonically with recorded
tool output, snapshots, and compaction summaries.

**When you hit it:** A long-lived deployment with heavy tool output.

**Workaround:** None in the library. Size the store for retention, or write an
offline sweep against your backend that walks the remaining streams' refs.
`delete_task` also refuses with `reason="running"` while any task in the tree
holds a live lease, so a purge never races an in-flight turn.

## Sandbox boundaries

### The library ships no sandbox provisioner

**What it means:** `SandboxProvider` is a protocol the SDK defines and drives
(through `SandboxExecEnvManager`), not an implementation it ships. The only
provider in the box adapts a `SandboxExecEnvConfig` into an **attach-only**
provider: it connects to one already-running container, and its `release` is a
no-op because it does not own the container. Provisioning and reaping — running
`docker`, calling a K8s API, choosing mounts — belong to the host.

**When you hit it:** You expected a fresh container per conversation out of the
box.

**Workaround:** Implement `SandboxProvider` in your host and pass it as
`HostConfig.sandbox_provider`. `allocate` returns a `SandboxHandle` carrying
addressing plus a live `SandboxAuth` strategy; `attach` reconnects to the
`exec_env_ref` recorded on `TaskHostBound`, which is how a resumed or reclaimed
session finds its container again. Whether that reconnect works across machines
is a property of the provider you write, not of the SDK.

### Sandbox side effects are not fenced across worker generations

**What it means:** When a session runs in a sandbox container, its file and
shell side effects go to the container over HTTP — outside the shared Postgres
transaction that fences EventLog writes. A worker fenced out of the log (a GC
pause, a `SIGSTOP` then revive) can still `POST` to the container. The sandbox
side effect is therefore at-least-once and unfenced, the same class as a
half-run `shell_run` on the host: a reclaiming worker reconnects to the same
container and re-drives the step, but a slow zombie can pollute the container in
the meantime. Because a container belongs to one root-task tree, a zombie
pollutes only its own session.

**When you hit it:** A worker holding a sandbox session stalls long enough for
its lease to expire and another worker to reclaim the task, then wakes and
issues one more container call.

**Workaround:** None automatic. It is bounded by the same step-attempt re-drive
and human review that cover crashed-step side effects above.

### Sandbox `shell_run` has no remote hard-kill

**What it means:** On the host, `shell_run`'s `timeout` maps to a real
subprocess timeout that kills the process. Under a sandbox there is no remote
cancel verb, so the timeout is enforced *client side* by the HTTP read timeout
of that one call. The `timeout` you pass is honoured — a command that runs past
it is reported to the model as a timed-out run at the requested budget — but
the command **keeps running in the container** after the call returns. Its side
effects may land after the tool has already reported a timeout.

**When you hit it:** A sandbox `shell_run` whose command exceeds its `timeout`
— a hanging build or test run.

**Workaround:** Treat a timed-out sandbox `shell_run` as "may still be
running"; a follow-up command can observe or clean up its partial effects. Give
genuinely long commands an explicit larger `timeout` so the client does not cut
the call off early.

### Background shell is host-only

**What it means:** `shell_run(run_in_background=true)` hands the validated argv
to the host's background runner and returns a job id that `shell_poll` and
`shell_kill` then address. A sandbox `ExecEnv` reports that it does not support
background execution, and the tool returns an error rather than running the
command in the foreground.

**When you hit it:** A sandboxed agent tries to start a long-running server or
watcher.

**Workaround:** Run the command in the foreground with a generous `timeout`, or
run the session outside the sandbox when background jobs are essential.

### The sandbox browser is text-level and container-scoped

**What it means:** A sandbox session can drive the container's headless browser
through five Noeta-owned tools (`browser_navigate`, `browser_click`,
`browser_type`, `browser_extract`, `browser_screenshot`). Three boundaries:

- **No browser without a container.** The pack mounts only when a live browser
  backend is in the session's backend bag *and* the agent activates `browser`.
  Otherwise the tool set is byte-identical to a non-browser session.
- **Perception is text and element level, not visual.** `browser_extract`
  returns page text plus a numbered list of interactive elements the model
  clicks and types by index. `browser_screenshot` saves a PNG as a workspace
  artifact; it is **not** fed back to the model as vision, so pages that need
  visual understanding are not fully handled.
- **The browser lives with the container.** It shares the session container's
  lifecycle and cost; there is no separate pause.

**When you hit it:** A task that must read a chart rendered only as pixels, or
one that needs to browse without a container.

**Workaround:** Prefer `browser_extract` for content and `webfetch` for pages
that need no interaction; use `browser_screenshot` when a human needs to look.

## Closed extension points

### The composer cannot be replaced

**What it means:** `ContextComposer` is a closed extension point on the user
surface. Stable-prefix KV-cache reproducibility is a hard constraint, so
swapping the composer wholesale is not offered. The open hooks are
registry-only and append-only: a `ContentKindSpec` (a semi-stable resident) or
a compose-time `reminder` (the dynamic-suffix tail). Neither touches the stable
prefix.

**When you hit it:** You want a fundamentally different prompt layout.

**Workaround:** Add residents and reminders through the open surfaces, or move
the decision into a custom `Policy`, which *is* replaceable through the
`policy` surface.

## Next steps

- [Troubleshooting](troubleshooting.md) — symptom → cause → fix for actual faults
- [Architecture overview](../architecture/overview.md) — the full system picture
- [State and writers](../architecture/state-and-writers.md) — the invariants
  these boundaries follow from
- [WorkerLoop reference](../reference/worker-loop.md) — constructor knobs and
  shutdown behavior
