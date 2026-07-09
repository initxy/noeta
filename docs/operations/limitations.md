# Known limitations

These are deliberate boundaries of the current preview, not bugs. Each
entry describes what it means, when you hit it, and the workaround if
any.

## Multi-host coordination requires Postgres

**What it means:** Single-host multi-worker is shipped — the agent runs a
resident `WorkerLoop` pool (`NOETA_AGENT_NUM_WORKERS`, default 1), so
several tasks progress at once on one host. Multiple *hosts* sharing one
database is supported only on **Postgres**: emit appends are fenced
in-transaction against the live lease, lease expiry runs on the database
clock (so per-host clock skew cannot split-brain), and a `worker_id`
column records the holder. The **SQLite** and **in-memory** backends stay
single-host — they have no cross-host fencing, so pointing two host
processes at one SQLite file is unsafe.

**When you hit it:** You want worker processes on more than one machine
draining a shared store.

**Workaround:** Use the Postgres backend for multi-host deployments. On
SQLite, keep to a single host (a multi-worker pool on that host is fine),
or give separate workload profiles their own SQLite files — there is no
cross-store routing in the ready queue, so tasks in one store are not
visible to workers draining another.

**Why it is this way:** Cross-host fencing needs a shared transactional
clock and lease arbitration the embedded stores do not provide; Postgres
supplies both. The design is recorded in the multi-host lease fencing ADR.

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

## Sandbox side effects are not fenced across worker generations

**What it means:** When a session runs in a sandbox container (the
`ExecEnv` seam bound to an AIO Sandbox backend), its file and shell
side effects hit the container over HTTP — outside the shared Postgres
transaction that fences EventLog writes. A worker that was fenced out of
the log (a GC pause, a `SIGSTOP` then revive) can still `POST` to the
container. The sandbox side effect is therefore **at-least-once and
unfenced**, the same class as a half-run `shell_run` on the host: a
reclaiming worker reconnects to the same container and re-drives the
step, but a slow zombie can pollute the container in the meantime. The
per-session-container model (2026-07-08) shrinks the blast radius — a
zombie now pollutes only **its own session's** container, not a
host-shared one — but the write is still unfenced across generations.

**When you hit it:** A worker holding a sandbox session stalls long
enough for its lease to expire and another worker to reclaim the task,
while the stalled worker later wakes and issues one more container call.

**Workaround:** None automatic in v1 — it is bounded by the same
step-attempt re-drive and human review that cover crashed-step side
effects (see "Mid-step crash recovery does not undo side effects"). A
future generation-token fence (the seam already reserves the slot) will
close the window.

**Why it is this way:** The lease-fencing design assumes no load-bearing
write lands outside the shared database; a container write is exactly
such a write. Fencing it properly needs an orchestration-layer token and
a validating proxy in front of the container — real work deferred to the
per-container future. The trade-off is recorded in the execution
environment seam ADR, which links back to the multi-host lease fencing ADR.

## Per-session sandbox: weak mount isolation, no cross-machine reconnect, idle cost

**What it means:** The per-session sandbox (2026-07-08) provisions a
**fresh container per root-task tree** (`LocalDockerSandboxProvider`), so
two concurrent conversations get **separate** containers and the durable
`exec_env_ref` carries a distinct `sandbox_id`. Three costs remain with
the local-Docker backend:

- **Weak filesystem isolation.** The container writes the host workspace
  through a `-v` bind mount, not a full FS jail; only the workspace + skill
  dirs are mounted (never host root), so a tool cannot reach outside those,
  but a write to a mounted path lands on the host filesystem directly.
- **No cross-machine reconnect.** A local-Docker container is bound to the
  machine that ran it. If a session is reclaimed on a **different** host, the
  `attach` fails — that host cannot reach a container on the original
  machine. Same-host resume/reclaim works.
- **Idle container cost + cold start.** A container has no pause/snapshot: it
  stays alive (and billed) while its session is active, including long
  suspends; each new session pays a seconds-scale AIO cold start.

**When you hit it:** Untrusted code that must not touch the host
filesystem at all; a multi-host deployment where a worker crashes and the
session is reclaimed on another machine; leaving many sandboxed sessions
suspended.

**Workaround:** For hard isolation, run the whole host in a VM/container.
For cross-machine reclaim, keep a session's reclaim on its original host
(the same-host path works), or use a distributed / NAS-backed provider
(below). For idle cost, close sessions you are done with so their
containers are released.

**Why it is this way:** The seam is built for a **Distributed** provider
family (TAE / K8s) that removes cross-machine reconnect from the storage
layer — a NAS mount makes file state reachable from any host, so a
reclaiming host just re-pulls a container against the same NAS. The three
zero-rework hooks are already in place: `SandboxHandle.auth` (a strategy,
not a static key — a short-lived Bearer JWT drops in), a gateway-path-prefix
`base_url`, and `MountSpec.kind=nas`. Real FS isolation (copy-in/sync-out)
and warm pool / pause are future provider work. See the execution
environment seam ADR (v2).

## Sandbox `shell_run` timeout is client-side, not a remote hard-kill

**What it means:** On the host, `shell_run`'s `timeout` maps to a real
subprocess timeout that kills the process. Under a sandbox the AIO
container has no remote hard-kill, so the timeout is enforced *client
side* by the HTTP read timeout of that one call. The `timeout` you pass
is honoured — a command that runs past it is reported to the model as a
timed-out run at the requested budget (not a fixed adapter default) — but
the command itself **keeps running in the container** after the call
returns, until the AIO lease cap reaps it. Its side effects may still
land after the tool has reported a timeout.

**When you hit it:** A sandbox `shell_run` whose command exceeds its
`timeout` (or the default) — for example a build or test run that hangs.

**Workaround:** None automatic in v1. Treat a timed-out sandbox
`shell_run` as "may still be running"; a follow-up command can observe
or clean up its partial effects. Give genuinely long commands an explicit
larger `timeout` so the client does not cut the call off early.

**Why it is this way:** AIO's `/v1/shell/exec` is a synchronous call with
no cancel verb, so the only bound the client owns is its own read
timeout. Threading the per-command budget into that timeout makes the
model-facing `timeout` behave like the local backend within the limits of
a no-hard-kill backend. A container-durable, cancellable job handle is
separate future work (the same work that unlocks background shell under a
sandbox). See the execution environment seam ADR.

## Sandbox browser is text-level and container-scoped in v1

**What it means:** A sandbox session can drive the container's headless
browser through five noeta-owned tools (`browser_navigate` /
`browser_click` / `browser_type` / `browser_extract` /
`browser_screenshot`), exposed to a `web` subagent the main agent
delegates page work to. Three v1 boundaries:

- **No browser without a container.** The tools appear only in sandbox
  mode (`NOETA_AGENT_SANDBOX`) for a browser-capable agent; a
  non-sandbox session has no browser at all.
- **Perception is text / element-level, not visual.**
  `browser_extract` returns page text plus a numbered list of
  interactive elements the model clicks/types by index;
  `browser_screenshot` is saved as a **workspace artifact** (viewable
  in the file panel), **not** fed back to the model as vision. Sites
  that need visual understanding (canvas, heavy anti-bot) are not fully
  handled — a config-gated vision mode is future work.
- **The browser lives and is billed with the container.** It shares
  the per-session container's lifecycle and idle cost (see above);
  there is no separate pause.

**When you hit it:** A task that must read a chart rendered only as
pixels, or one that needs to browse without a sandbox container.

**Workaround:** For content, prefer `browser_extract` (and `webfetch`
for raw pages that need no interaction); for a visual record,
`browser_screenshot` saves a PNG a human can open. Enable the sandbox to
get the browser at all.

**Why it is this way:** v1 pins a text/element-level contract that keeps
the model's context lean and the tool schema stable; visual perception
and the coordinate-level `/v1/browser` path are a deliberate increment 2
with the seam already reserved. See the execution environment seam ADR
(browser subsystem).

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
