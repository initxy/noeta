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

## Mid-step crash recovery does not undo side effects

**What it means:** A worker crash **mid-step** (`kill -KILL`, power
loss) is recovered automatically on the next lease: the interrupted
attempt is sealed with a durable `StepAttemptAbandoned` marker and the
step is re-driven when everything the attempt recorded would have run
without a human approval gate. When the attempt had unprovable side
effects — or after 3 consecutive seals in one turn — the task is
instead **parked**: suspended as a stopped conversation with an
`origin="system"` notice naming each interrupted call and whether it
completed. A crash during a human-approved tool execution always parks,
re-suspended on the same approval. Recovery never silently terminates
the task and never silently re-runs a side-effectful call — but it
also does **not** undo anything the crashed attempt already did.

**When you hit it:** A hard kill or power loss lands during an attempt
that had already run side-effectful tools (an interrupted `shell_run`,
a completed `edit`, an approved call mid-execution). Normal shutdown
(SIGTERM) does not trigger this, and a crash during reads or planning
recovers with no human involved.

**Workaround:** Open the parked conversation — the notice lists what
was interrupted. Verify whether those operations applied fully,
partially, or not at all, then just type to continue (the turn resumes
from the clean pre-attempt baseline) or re-approve the pending call;
`close` / `cancel` work as usual.

**Why it is this way:** Classification reuses the same permission
surface that gates live execution, so recovery is never more permissive
than the agent's own approval rules. But whether a half-run `shell_run`
actually changed the world cannot be proven from the log — the design
prevents silent duplicates and leaves the judgment of half-applied
effects to a human.

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
`heartbeat_invalid_lease`, `shutdown_abandoned`, `timers_fired`,
`attempt_abandoned`, `attempt_parked` — to an injectable sink
(default: structured logs). These are **not** EventLog events, are not
persisted, and do not survive a process restart.

**When you hit it:** You are trying to build monitoring or alerting on
top of worker reliability signals.

**Workaround:** Mount a custom `reliability_sink` that forwards events
to your monitoring system. Each event is named for what the worker can
prove from the dispatcher seam (e.g. `heartbeat_invalid_lease` is a
symptom — the cause may be cap / expired / requeued).

## No out-of-band notification for human-in-the-loop waits

**What it means:** Human-in-the-loop is fully wired in-band: the engine
suspends on `HumanResponseReceived` wake events, the `answer` /
`approve` / `deny` client verbs deliver the response, and the bundled
web UI renders structured question forms (choices plus freeform) and
approval prompts. What does not exist is an out-of-band channel — no
webhook, email, or cross-session inbox fires when a task starts waiting
on a human.

**When you hit it:** An agent asks a question or requests approval
while nobody has the chat open. The task waits durably (that is the
point), but nothing notifies anyone that it is waiting.

**Workaround:** Keep the web UI open for interactive sessions, or in
headless deployments poll `GET /tasks` for suspended tasks (and answer
programmatically via `client.answer(task_id, question_id=...,
answers=...)`). A custom `Observer` subscribed to the EventLog can
forward `UserQuestionRequested` / `ToolCallApprovalRequested` events to
your own notification channel.

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
