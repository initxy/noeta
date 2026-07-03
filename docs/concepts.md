# Concepts

Noeta is built around a small set of primitives. This page introduces
each one and how they fit together. The single source of truth for
vocabulary is [`CONTEXT.md`](../CONTEXT.md); architectural decisions
live under [`docs/adr/`](adr/).

## Task

A `Task` is the only primitive (docs/adr/task-as-only-primitive.md). It is an addressable unit
of agent work — it has a `task_id`, a `status` (`pending` / `running`
/ `suspended` / `terminal`), and a `parent_task_id` if it was spawned
by another task. State is reconstructed by folding the EventLog;
the Engine never holds task state in memory across Engine runs.

## EventLog

Per-task append-only stream of `EventEnvelope` records (docs/adr/event-sourced-truth.md).
Every state change emits an envelope: `TaskCreated`, `MessagesAppended`,
`LLMRequestStarted`, `ToolCallStarted`, `TaskSuspended`, `TaskWoken`,
`TaskCompleted`, and so on. The EventLog is the single source of
truth — there is no separate "task table" the Engine reads.

Implementations:

* `InMemoryEventLog` — for tests and `NOETA_AGENT_SQLITE_PATH=:memory:`
* `SqliteEventLog` — durable, WAL-mode sqlite3 file

Both implement the same `EventLog` Protocol (docs/adr/storage-protocols-l0.md).

## ContentStore

Content-addressed, dedup-by-hash blob store (docs/adr/event-sourced-truth.md). Bodies larger
than the 4 KB event-payload cap are uploaded here; the envelope only
carries a `ContentRef(hash, size, media_type)`. Examples: full LLM
request/response bodies, large tool outputs.

## Dispatcher

Owns the lease-per-segment Worker model (docs/adr/worker-lease-model.md). Workers call
`enqueue → lease → (heartbeat*) → release / fail` to drive ready
tasks; `wake` requeues suspended tasks. The Dispatcher also acts as
the `LeaseRegistry` the EventLog consults on every `emit(lease_id=…)`
to enforce docs/adr/single-writer-invariant.md single-writer.

## Engine

Stateless step driver. `run_one_step(task, lease_id=…)` advances a
task by one Policy decision: it composes a context, runs Guards, asks
the Policy for a `Decision`, applies the Decision's effects (tool
calls, LLM round-trips, subtask spawn, suspend, terminate), and emits
envelopes. docs/adr/guard-observer-hooks.md caps the Engine class body at 500 lines so it
stays readable.

## Policy

Returns a typed `Decision` from a folded task view: `ToolCallsDecision`,
`FinishDecision`, `FailDecision`, `SpawnSubtaskDecision`,
`WaitTimerDecision`, `YieldForHumanDecision`. The ReAct policy is the
production policy; the stub policies (`StubFinishPolicy`,
`StubScriptedPolicy`) are deterministic test doubles.

## Composer

Pure function from `RuntimeState` (folded) to a three-segment context
(stable_prefix / semi_stable / dynamic_suffix). The Composer is
called once per `run_one_step` and writes a `ContextPlanComposed`
envelope recording exactly what context the step was built from.

## Guard / Observer

docs/adr/guard-observer-hooks.md's two hook surfaces.

* **Guards** sit on the Engine's hot path. `BudgetGuard` and
  `PermissionGuard` ship in-tree. Guards can deny a tool call, deny
  a subtask spawn, or force a budget-exhaustion failure.
* **Observers** subscribe to the EventLog via `subscribe(callback)`.
  Callbacks run synchronously *after* each envelope is durable, on
  the writer thread but outside the writer lock. `AuditObserver`,
  `MetricsObserver`, `EventFanout`, and `ChildLifecycleObserver`
  ship in-tree.

## Fold-based state reconstruction

Because the EventLog is the single source of truth, a task's full state
is **deterministically folded** from it (snapshot-accelerated) at every
wake / SSE reconnect / inspect. This rebuild is the backbone of
suspend/resume and multi-turn conversation; it folds forward only and
never re-calls a provider.

## Wake-resume

<p align="center">
  <img src="assets/task-lifecycle.svg" alt="Task lifecycle — unified suspension, wake events, and terminal exits" width="820">
  <br>
  <em>The task lifecycle: all waiting is one <code>Suspended</code> state plus a typed wake condition; a wake event re-enqueues the task for the next lease.</em>
</p>

When a task suspends with a typed `WakeCondition`
(`SubtaskCompleted` / `HumanResponseReceived` / `TimerFired`), the
Dispatcher matches incoming wake events by **projection** — only
identity fields participate in the match (e.g. `subtask_id` for
subtasks; `handle` for human responses; `fire_at` for timers, with
threshold semantics `event.fire_at >= condition.fire_at`).

The matched event is handed to the worker on the next `lease()` via
`Lease.wake_event` and threaded into `Engine.note_woken` to write a durable
`TaskWoken(wake_event=…)` envelope before continuing. Delivery is
**single-worker durable exactly-once** (H2 / docs/adr/subtask-fanout-and-durable-wake.md): the matched wake
**survives the lease** and is cleared only by a consuming
`release(consumed_wake_event=…)`, so a worker crash between lease and the
`TaskWoken` write no longer loses it — `requeue_stale()` brings the task back
to ready with the wake preserved and the next lease re-delivers it.
Consumption is idempotent: the worker's woken branch is a recovery state
machine keyed on the latest matching `TaskWoken`, so a re-delivery whose
`TaskWoken` already landed is reconciled without emitting a second one. No
operator re-issue is needed. A targeted resume (HTTP `POST /tasks/{id}/resume`)
on a task with no queued wake returns a typed `suspended_without_wake_event`
(it is simply
waiting for a wake that has not occurred yet — a diagnostic, not a loss).
Scope is single-host / single-worker; the partial-step-orphan edge (a crash
mid-step) and multi-worker / multi-host concurrency remain limitations. See
[`docs/failure-modes.md`](failure-modes.md).

## How a step flows

<p align="center">
  <img src="assets/turn-sequence.svg" alt="One turn of task execution — goal submission, lease, step loop, finish, streamed over SSE" width="820">
  <br>
  <em>One full turn through the bundled agent: submit → lease → step loop → finish. The SSE stream on the left is the product surface; steps 1–6 below are the runtime-level slice of the same picture.</em>
</p>

1. A Worker calls `dispatcher.lease(...)` and gets
   back a `Lease(lease_id, task_id, expires_at, wake_event?)`. The drain
   loop is the library primitive `noeta.runtime.worker.WorkerLoop` (the
   shipped operator CLI worker was removed in TL6; embedders run
   `WorkerLoop(...).run_forever(...)` in-process).
2. Worker folds the EventLog into a `RuntimeState`.
3. If `lease.wake_event` is set, Worker calls `engine.note_woken(task,
   lease_id, wake_event=...)` which writes `TaskWoken`.
4. Worker calls `engine.run_one_step(task, lease_id=...)`. Engine:
   * runs the Composer → `ContextPlanComposed`
   * calls registered Guards (`pre_decide` / `pre_tool_call`)
   * asks the Policy for a `Decision`
   * dispatches on the Decision type — each handler writes its
     envelopes through the lease-validated EventLog
5. Worker calls `dispatcher.release(lease_id, next_state=…,
   wake_on=…)` (or `dispatcher.fail(...)`).
6. Observers see envelopes after each successful `emit`. They run
   sync on the writer thread but outside the writer lock, so any
   Observer exception is swallowed.
