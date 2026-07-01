# Failure modes

Common failures and how to recover.

## Missing API key

A `python -m noeta.agent` server configured with
`NOETA_AGENT_PROVIDER=openai` (or `anthropic`) but no credential will exit
at boot with:

```text
NOETA_AGENT_PROVIDER='openai' needs NOETA_AGENT_API_KEY
```

Recover by setting `NOETA_AGENT_API_KEY` in the environment (or under
`api_key` in a `NOETA_AGENT_CONFIG` JSON file). OpenAI additionally needs
`NOETA_AGENT_BASE_URL` (else boot exits with
`NOETA_AGENT_PROVIDER='openai' needs NOETA_AGENT_BASE_URL`); Anthropic uses
the same `NOETA_AGENT_API_KEY` and treats `NOETA_AGENT_BASE_URL` as optional.

The default `NOETA_AGENT_PROVIDER=stub` bypasses this requirement and is
the right choice when you only want to test wiring.

## Budget exhaustion

`BudgetGuard` denies a `ProposedAction` (tool call, subtask spawn,
or finish) when any configured budget axis has been crossed —
iterations, tool calls, cost USD, spawned subtasks. The guard
returns a `VerdictResult.deny(reason=...)` with a per-axis reason
string such as `"max_iterations=5 exceeded"` or
`"max_tool_calls=3 reached"`. Depending on which action was being
proposed, the Engine emits either a guard-denial envelope
(`ToolCallDenied` / `SubtaskDenied`) or, when the iteration or
cost cap fires before any allowed action remains, a `TaskFailed`
envelope. The exact reason is whatever string the BudgetGuard
returned — there is no fixed `budget_exhausted_*` taxonomy.

A budget-exhausted task still ran and produced durable envelopes; it
just terminated unsuccessfully. Resuming a terminal task returns the
typed `reason: terminal` failure.

Recover by:

* inspecting the recording (the EventLog read models / the code
  session's inspect projection) to read the exact denial reason in
  the relevant denial / `TaskFailed` envelope
* raising the budget — the code runner's default lives in
  `noeta.agent.host.session.default_coding_budget()` (the budget
  `python -m noeta.agent` sessions use when no explicit one is
  passed); programmatic callers pass a `Budget(...)` via
  `CodeSessionConfig(budget=...)`. (The
  `noeta.testing.profile.default_budget()` helper is the
  test/demo default only — production never imports
  `noeta.testing`.)
* trimming the task's scope to require fewer steps

## Permission denial

`PermissionGuard` rejects a `ToolCallsDecision` or
`SpawnSubtaskDecision` that requests a denied tool or agent. The
Engine emits `ToolCallDenied` / `SubtaskDenied` envelopes; the
policy sees the denial in its next decide round.

Recover by widening the permission policy
(`PermissionPolicy.allowed_tools` / `allowed_subtask_agents`) or
changing the task's goal to avoid the denied action.

## Durable exactly-once wake (H2)

When a suspended task is woken via `dispatcher.wake(...)`, the matched
event lives on the dispatcher row. **H2 (docs/adr/subtask-fanout-and-durable-wake.md) makes wake delivery
and consumption exactly-once across a crash** (single-host /
single-worker): the matched wake **survives `lease()`** (it is no longer
destroyed at lease time), is cleared only by a **consuming release** that
presents the consumed wake (after the durable `TaskWoken` is written), and
is otherwise **re-delivered** by `requeue_stale()` after a crash. So a
worker crash between `lease()` and the `TaskWoken` write no longer loses
the wake — `requeue_stale()` brings the task back to ready **with the wake
preserved**, and the next lease re-delivers it.

Consumption is idempotent: the worker's woken branch is a recovery state
machine keyed on the latest matching `TaskWoken` within the current
suspend-window. A re-delivery whose `TaskWoken` was already written is
reconciled (terminal / re-suspended / continue) **without emitting a second
`TaskWoken`**; one whose `TaskWoken` was not yet written emits exactly the
first. Net: *the wake that should fire always fires; a re-delivered wake is
consumed only once.* **No operator re-issue is needed** (the former manual
`dispatcher.wake(...)` recovery recipe is obsolete).

Scope: single-host / single-worker. Multi-worker concurrency (concurrent
re-delivery, fencing, completion-ordering) is a future slice. And a step
that crashes **mid-flight** — after `TaskWoken`, with partial step events,
still `running` — is the **partial-step-orphan** limitation below: H2 does
not silently re-run a partial step (the worker raises a typed
`PartialStepOrphan`).

## Resident worker loop (`WorkerLoop`)

The single-host resident drain loop is no longer a shell command —
`noeta serve` was removed in TL6. The drain loop is now the **library
primitive** `noeta.runtime.worker.WorkerLoop`: an embedder constructs it
and calls `WorkerLoop(rt, ...).run_forever(install_signals=True)` (the
`install_signals=True` flag wires SIGTERM/SIGINT onto `loop.stop()` via
`noeta.runtime.worker.install_stop_signals`). Nothing Noeta ships launches
it. The chat-server / UI use-case that `noeta serve` once doubled as is
now `python -m noeta.agent` (an env-configured HTTP/SSE server + bundled
SPA — it starts **no** `WorkerLoop`; delegation is off). See
[`daemon.md`](daemon.md) for the loop's model and full limitation list.
The two recovery paths an embedder hits:

**Wake under the loop (durable exactly-once, H2).** A worker crash
between leasing a woken task and writing `TaskWoken` no longer loses the
wake: the matched wake survives the lease and `requeue_stale()`
re-delivers it, and consumption is idempotent (see
[Durable exactly-once wake (H2)](#durable-exactly-once-wake-h2) above). No
operator re-issue is needed. Single-host / single-worker scope; multi-worker
is a future slice.

**Stuck step.** Shutdown is **bounded process-shutdown** (H1):
SIGTERM/SIGINT flips `loop.stop()` (the loop notices at the top of its
next iteration after the current synchronous step finishes), then
`run_forever` waits up to the loop's `shutdown_grace_s` for the
in-flight step; if it does not finish, the loop **abandons** it (stops
its heartbeat, emits `shutdown_abandoned`, sets `loop.abandoned`) WITHOUT
releasing or failing the lease, and `run_forever` returns. The host MUST
then **exit the process** — Noeta does **not** interrupt the running step
(Python cannot kill the thread), so abandon only takes effect because the
process exits, taking the abandoned daemon thread with it. The abandoned
lease then expires; the loop's periodic `requeue_stale()` sweep (run each
iteration via `maybe_sweep()`, cadence `stale_sweep_interval`) returns the
task to the ready queue — on the next process's loop, since the abandoning
process exits. (Construct the loop with `shutdown_grace_s=None` or `<= 0`
to select the old unbounded wait; a genuinely hung step then needs an
external `kill -KILL <pid>`.)

```bash
kill -TERM <pid>   # graceful: loop.stop() → finishes within grace, else
                   # abandons + the host exits the process
```

No **durable EventLog** state is lost — the recording up to the last
durable step is intact, and a lease-only / pre-`TaskStarted` crash
recovers byte-equal. (Caveats: a **partial-step** crash that left orphan
events — see [`daemon.md` → Crash recovery scope](daemon.md#crash-recovery-is-scoped-to-the-no-orphan-event-class) —
is a known limitation; and a tool/external API that produced a side
effect before the abandon/kill but never finished writing its EventLog
record may repeat that effect on retry — external-effect idempotency is
not solved here.)

If a step instead exhausts the heartbeat keepalive window
(`heartbeat_interval × heartbeat_max`), the lease is force-released and
the step's next EventLog write fails with `InvalidLease`. The loop logs
and continues. This cap-hit is an **operational-failure signal, not a
recovery path**: the loop cannot distinguish a cap-hit from a normal
lease expiry, so 3A makes **no promise** about the task returning to
ready or being picked up by a future lease. Inspect it — HTTP
`GET /tasks/{id}` (folded detail) and `GET /tasks/{id}/events` (envelope
history) against a running `python -m noeta.agent` server, or Python
`noeta.core.fold.fold(event_log, content_store, task_id)` — and decide
what to do by hand.

## Engine class body over budget

The Engine class body is capped at 500 lines (docs/adr/guard-observer-hooks.md). A PR that
crosses the line will fail the `test_real_engine_under_500_budget`
gate. Re-factor by moving handlers into `noeta/core/_decision_handlers.py`
following the C3 pattern.
