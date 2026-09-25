# Known limitations

Places where the shipped code deliberately stops, usually because going further would
mean the library owning something the host should own. None of these is a bug; for
faults, see [troubleshooting](troubleshooting.md).

## Process and deployment

### No process runs for you

- **Boundary:** `noeta-runtime` and `noeta-sdk` are libraries: no CLI, no HTTP/SSE
  server, no scheduler daemon. A task enqueued with no worker running just sits in the
  queue.
- **Workaround:** run a `WorkerLoop` yourself or call `Client.start_workers(n)`.
  `examples/reference-host` is the smallest host, built from the public surface only.

### Multi-host needs Postgres

- **Boundary:** several worker *processes* sharing one database are safe only on
  Postgres (appends fenced against the live lease in the same transaction, lease expiry
  on the database clock). SQLite and in-memory are single-host; two processes on one
  SQLite file is unsafe.
- **Workaround:** use Postgres across machines. On one host, a worker pool is fine, and
  any number of clients in one process can share storage — each has its own
  `HostConfig.queue`, children inherit it, and workers never cross queues. See ADRs
  [multi-host lease fencing](https://github.com/initxy/noeta/blob/main/docs/adr/multi-host-lease-fencing.md)
  and [worker queue routing](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md).

## Durability

### Crash recovery cannot undo side effects

- **Boundary:** after a hard kill mid-step, the interrupted attempt is sealed with a
  `StepAttemptAbandoned` marker. The step is re-driven only if everything it recorded
  would have run without approval; otherwise — or after 3 seals in one turn — the task
  is **parked**: suspended with an `origin="system"` notice listing each interrupted
  call and whether it completed. A crash during a human-approved tool always parks on
  the same approval. Recovery never re-runs a side-effectful call silently, but it
  cannot undo what already ran.
- **Workaround:** open the parked task, check whether the listed operations applied,
  then type to continue (the turn restarts from the pre-attempt state) or re-approve.
  Normal SIGTERM does not trigger this.

### Shutdown can leave a step running

- **Boundary:** `stop()` waits `shutdown_grace_s` for the in-flight step, then abandons
  it. Python cannot kill the thread; it may keep writing to the event log.
- **Workaround:** exit the process after an abandon. The lease expires and
  `requeue_stale()` reclaims the task. `shutdown_grace_s=None` (or `<= 0`) waits
  forever; a stuck step then needs `kill -KILL <pid>`.

### Heartbeat has a ceiling

- **Boundary:** one step holds its lease for at most `heartbeat_interval ×
  heartbeat_max` (360 by default — hours in practice). Past it the lease is released
  and the next write fails with `InvalidLease`.
- **Workaround:** treat a hit as a signal to inspect the task, not as recovery.

## Observability

### Reliability events are process-local

- **Boundary:** worker signals (`stale_requeued`, `suspended_without_wake`,
  `step_failed_retryable`, `heartbeat_invalid_lease`, `shutdown_abandoned`,
  `timers_fired`, `attempt_abandoned`, `attempt_parked`, `cap_terminal_reconciled`,
  `dispatcher_unavailable`) go to a sink that defaults to structured logs. They are not
  event-log events and do not survive a restart.
- **Workaround:** pass a `reliability_sink` that forwards them to your monitoring.

### Nobody is notified when a task waits on a human

- **Boundary:** the task suspends on a `HumanResponseReceived` wake condition and
  `answer` delivers the reply, but no webhook, email or inbox fires.
- **Workaround:** subscribe an `Observer`, forward `UserQuestionRequested` to your own
  channel, and reply with `answer`.

## Growth and cost

### Uncatalogued models: conservative compaction, $0 pricing

- **Boundary:** an unknown model gets a 128,000-token window and 16,384-token output
  cap (compaction stays on, but may run early) and a price of `0.0`, so
  `GovernanceState.cost` stays zero and `max_cost_usd` never fires. Each is logged once.
- **Workaround:** register a `ModelSpec` via `HostConfig(extra_models={...})` or
  `register_models` from `noeta.sdk.providers`.

### Content is never garbage-collected

- **Boundary:** the content store is content-addressed and append-only; no GC ships.
  `Client.delete_task` purges a task tree's events and dispatcher state but keeps the
  blobs, which may be shared by hash with other tasks. It refuses with
  `reason="running"` while any task in the tree holds a live lease.
- **Workaround:** size storage for retention, or write an offline sweep that walks the
  remaining streams' refs.

## Sandbox

### No sandbox provisioner ships

- **Boundary:** `SandboxProvider` is a protocol. The only built-in provider attaches to
  one already-running container (from `SandboxExecEnvConfig`); its `release` is a
  no-op. Creating and reaping containers is the host's job.
- **Workaround:** implement `SandboxProvider` and pass it as
  `HostConfig.sandbox_provider`. `allocate` returns a `SandboxHandle`; `attach`
  reconnects to the `exec_env_ref` on `TaskHostBound` when a task resumes. See
  [Sandbox](../guides/sandbox.md).

### Sandbox side effects are not fenced

- **Boundary:** container calls go over HTTP, outside the Postgres transaction that
  fences log writes. A worker that lost its lease (GC pause, `SIGSTOP`) can still reach
  the container — at-least-once, like a half-run host `Bash`. Damage stays inside that
  root task's own container.
- **Workaround:** none automatic; the same re-drive and human review as crashed steps
  apply.

### Sandbox `Bash` timeout does not kill the command

- **Boundary:** with no remote cancel, `timeout` is enforced by the HTTP read timeout.
  The model sees a timed-out run, but the command keeps running in the container.
- **Workaround:** treat a timeout as "may still be running" and check with a follow-up
  command; give long commands a larger `timeout`.

### Background shell is host-only

- **Boundary:** `Bash(run_in_background=true)` (with `BashOutput` / `KillShell`) needs
  the host's background runner. A sandbox returns an error instead.
- **Workaround:** run in the foreground with a generous `timeout`, or run outside the
  sandbox.

### Sandbox browser is text-level

- **Boundary:** the five tools (`browser_navigate`, `browser_click`, `browser_type`,
  `browser_extract`, `browser_screenshot`) mount only with a live browser in the
  container *and* the `browser` activation. `browser_extract` returns text plus numbered
  elements; `browser_screenshot` saves a PNG to the workspace but is not shown to the
  model. The browser shares the container's lifetime and cost.
- **Workaround:** use `browser_extract` for content, `WebFetch` for pages that need no
  interaction, and screenshots for humans.

## Closed extension points

### The context composer cannot be replaced

- **Boundary:** swapping `ContextComposer` would break the stable prompt prefix the
  provider cache depends on. Only append-only hooks are open: a `ContentKindSpec`
  resident or a compose-time `reminder`.
- **Workaround:** use those hooks, or replace the `Policy` through the `policy` surface.
  See [Context](../how-it-works/context.md).

## Next

- [Troubleshooting](troubleshooting.md)
- [How it works](../how-it-works/index.md)
- [`WorkerLoop` reference](../reference/worker-loop.md)
