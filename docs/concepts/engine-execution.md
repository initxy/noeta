# Engine & execution

The Engine is a **stateless step driver**: `run_one_step(task, lease_id=…)`
advances a Task to its next **suspend or terminal**, then returns. It holds no
task state across calls — the caller hands it a Task folded fresh from the
EventLog (see [Event sourcing](event-sourcing.md)).

"One step" is a *turn boundary*, not a single model round-trip. Inside one call
the Engine keeps looping: it composes a View, asks the Policy, lands the
Decision's effects, and — for every loop-continuing Decision — goes round again.
Only an exit Decision ends the call, moving the Task to `terminal` or
`suspended`. A step that runs ten tool calls is still one `run_one_step`.

<p align="center">
  <img src="../assets/turn-sequence.svg" alt="One turn of task execution — goal submission, lease, step loop, finish, streamed to a host UI" width="820">
  <br>
  <em>One full turn through an embedding host: submit → lease → step loop → finish. The whole step loop runs inside one <code>run_one_step</code> call.</em>
</p>

## One turn: compose → decide → dispatch

<p align="center">
  <img src="../assets/diagrams/engine-execution.svg" alt="Engine execution — compose → decide → dispatch, tool_calls loops" width="820">
</p>

1. **Compose.** The ContextComposer assembles the View — the exact input the
   model will see — from the folded state, and the Engine records a
   `ContextPlanComposed` envelope naming what the turn was built from (see
   [Composer & cache](composer-and-cache.md)). This happens once per turn of the
   loop.
2. **Decide.** The Policy reads the View and returns a typed `Decision`. The
   Policy is a pure function: it emits no events, touches no storage, and has no
   write access — it only states a position. `ReActPolicy` is the default;
   deterministic stub policies stand in for tests.
3. **Dispatch.** The Engine routes on the Decision type and lands its effects —
   tool calls, LLM round-trips, subtask spawns, suspension, termination — as
   envelopes through the lease-validated EventLog.

Guards run on this hot path and can veto an action before it happens (see
[Guard vs Observer](guard-observer.md)).

## The Decision vocabulary

The Policy speaks a small, neutral vocabulary, and the Engine routes each
Decision to one of three destinations:

| Route | Decisions | What happens |
| --- | --- | --- |
| Continue | `ToolCallsDecision`, `StatePatchDecision`, `CompactionRequestedDecision`, a background `SpawnSubtaskDecision` | emit the events, don't suspend, loop back to compose → decide |
| Suspend | a foreground `SpawnSubtaskDecision`, `SpawnSubtasksDecision`, `YieldForHumanDecision`, `WaitTimerDecision`, `WaitExternalDecision` | write a snapshot, emit `TaskSuspended`, release execution and wait to be woken |
| Terminate | `FinishDecision`, `FailDecision` | write a snapshot and a terminal event; the Task ends |

Two Decisions sit on a line. A `SpawnSubtaskDecision` with `background=True`
continues the turn when a background launcher is wired, and suspends on a barrier
otherwise. A `ToolCallsDecision` normally continues, but a Guard that demands
approval turns it into a suspend on the spot — the blocked call is recorded
first, so resume can reconstruct it exactly.

Splitting "stating a position" (Policy) from "posting to the ledger" (Engine) is
the single-writer invariant seen from the execution side: the right to decide is
open — swap in your own Policy — while the right to record stays closed, so even
a badly behaved Policy cannot corrupt ground truth.

## Boundaries the Engine keeps

The Engine knows nothing of Workers, the Dispatcher, or any transport — it
advances one Task by one step and stops. It is deliberately small: the control
flow only routes Decisions, delegating the actual work to per-Decision handlers.
Cancellation is cooperative: an optional `cancelled` predicate is polled at the
top of each turn and again immediately after the Policy decides, so a cancel that
lands mid-round-trip abandons the result instead of interrupting a thread. And a
Policy that never yields still gets resume points — the Engine writes a snapshot
every 20 consecutive tool-call turns.

Related: [Task model](task-model.md) ·
[Wake & resume](wake-resume.md) ·
[Architecture overview](../architecture/overview.md)
