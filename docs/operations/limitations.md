# Known limitations

These are deliberate boundaries of the current preview, not bugs. Each
entry describes what it means, when you hit it, and the workaround if
any.

## Single-host / single-worker only

**What it means:** The shipped deployment shape is one durable store
(SQLite) and one resident `WorkerLoop` process draining it. There is no
`workers` knob on `WorkerLoop` — throughput is one step at a time.

**When you hit it:** You need to scale task throughput beyond what a
single worker can handle, or you want to run workers on multiple hosts
against a shared database.

**Workaround:** Give different workload profiles their own SQLite files
and run separate worker processes. There is no cross-process routing in
the ready queue — tasks in one store are not visible to workers
draining another.

**Why it is this way:** Multi-worker coordination requires fencing
between competing workers (so two workers do not lease the same task),
distributed timer due-checks, and completion-ordering guarantees. That
is a significant architectural slice, not yet shipped.

## Partial-step-orphan crash edge

**What it means:** If a worker crashes **mid-step** — after writing
`TaskWoken` and some partial step events (e.g. `ToolCallStarted` but
not `ToolCallCompleted`) — the task is left in a state with orphan
events. The fold can rebuild state, but a from-scratch replay does not
reproduce the partial attempt.

**When you hit it:** The process is killed (`kill -KILL`) or the host
loses power during a step that has already written some events. Normal
shutdown (SIGTERM) does not trigger this — the grace window and
heartbeat handle that.

**Workaround:** Inspect the task manually (`GET /tasks/{id}/events`)
and decide whether to re-drive it or close it. The worker raises a
typed `PartialStepOrphan` error when it detects this on resume; it does
not silently re-run the partial step.

**Why it is this way:** Closing this cleanly needs an attempt-journal /
replay-semantics mechanism — its own ADR. The current design prioritizes
not silently corrupting state over automatic recovery from mid-step
crashes.

## Bounded shutdown, but no in-process thread interrupt

**What it means:** On `stop()` (SIGTERM / SIGINT), `WorkerLoop` waits up
to `shutdown_grace_s` for the in-flight step to complete. If it does
not finish, the loop **abandons** the step and returns. But Python
cannot kill the abandoned step thread — it may still be running and
writing to the EventLog.

**When you hit it:** A step hangs (e.g. a tool call to an unresponsive
external API) and the grace window expires.

**Workaround:** **Exit the process.** After abandon, the host must call
`sys.exit()` or equivalent. The abandoned thread dies with the process;
its lease expires and `requeue_stale()` reclaims the task on the next
start. Set `shutdown_grace_s=None` (or `<= 0`) for unbounded wait —
then a stuck step needs external `kill -KILL <pid>`.

## Heartbeat keepalive window is capped

**What it means:** The heartbeat keeps a slow step's lease alive, but
not forever. The dispatcher caps heartbeat extensions
(`heartbeat_max`), so `heartbeat_interval × heartbeat_max` is the
maximum time one step can hold a lease. Past the cap, the lease is
force-released and the step's next EventLog write fails with
`InvalidLease`.

**When you hit it:** A single step (one LLM turn plus all its tool
calls) takes longer than the cap window. The default is generous
(hours, not minutes), so this is rare.

**Workaround:** This cap-hit is an **operational-failure signal, not a
recovery path**. The loop logs and continues to the next task, but the
cap-hit task may need operator inspection. Check if the task is still
viable or if it should be closed.

## Reliability events are process-local, not durable

**What it means:** The worker emits `ReliabilityEvent`s —
`stale_requeued`, `suspended_without_wake`, `step_failed_retryable`,
`heartbeat_invalid_lease`, `shutdown_abandoned` — to an injectable sink
(default: structured logs). These are **not** EventLog events, are not
persisted, and do not survive a process restart.

**When you hit it:** You are trying to build monitoring or alerting on
top of worker reliability signals.

**Workaround:** Mount a custom `reliability_sink` that forwards events
to your monitoring system. Each event is named for what the worker can
prove from the dispatcher seam (e.g. `heartbeat_invalid_lease` is a
symptom — the cause may be cap / expired / requeued).

## No durable human-in-the-loop UX yet

**What it means:** The engine carries the full shape for human
intervention (`HumanResponseReceived` wake events, the `answer` /
`approve` / `deny` client verbs), but the end-to-end UX for a human
responding to an agent's question is still landing.

**When you hit it:** You want to build an agent that asks the user a
question mid-task and waits for a response.

**Workaround:** Use the programmatic API: `client.answer(task_id,
question_id=..., answers=...)` to inject a human response into a
waiting task. The web UI does not yet surface a structured question /
answer flow.

## Frontend is a small Vite MPA, not a framework app

**What it means:** The shipped web app (`/chat`, `/trace`) is a small
Vite multi-page app with vanilla ES modules. There is no React / Vue /
Svelte migration planned for the preview.

**When you hit it:** You want to build a complex UI on top of the
agent.

**Workaround:** The [HTTP API](../reference/http-api.md) is the
integration surface. Build your own frontend against it, or embed the
agent via the SDK directly.

## See also

- [Troubleshooting](troubleshooting.md) — symptom → cause → resolution
- [Wake & resume](../concepts/wake-resume.md) — the delivery guarantee
  and its scope
- [WorkerLoop reference](../reference/worker-loop.md) — constructor
  knobs and shutdown behavior
- [Architecture overview](../architecture/overview.md) — the full
  system picture
