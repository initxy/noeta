# Engine & execution

The Engine is a **stateless step driver**: `run_one_step(task, lease_id=…)`
advances a Task by exactly one Policy decision, then returns. It holds no task
state across calls — every step begins from a fresh fold of the EventLog (see
[Event sourcing](event-sourcing.md)).

<p align="center">
  <img src="../assets/turn-sequence.svg" alt="One turn of task execution — goal submission, lease, step loop, finish, streamed over SSE" width="820">
  <br>
  <em>One full turn through the bundled agent: submit → lease → step loop → finish. Each iteration of the step loop is one <code>run_one_step</code>.</em>
</p>

## One step: compose → decide → dispatch

1. **Compose.** The ContextComposer assembles the View — the exact input the
   model will see — from the folded state, and a `ContextPlanComposed`
   envelope records what the step was built from (see
   [Composer & cache](composer-and-cache.md)).
2. **Decide.** The Policy reads the View and returns a typed `Decision`. The
   Policy is a pure function: it emits no events, touches no storage, and has
   no write access — it only states a position. The production Policy is
   ReAct; deterministic stub policies stand in for tests.
3. **Dispatch.** The Engine routes on the Decision type and lands its
   effects — tool calls, LLM round-trips, subtask spawns, suspension,
   termination — as envelopes through the lease-validated EventLog.

Guards run on this hot path and can veto an action before it happens (see
[Guard vs Observer](guard-observer.md)).

## The Decision vocabulary

The Policy speaks a small, neutral vocabulary — `ToolCallsDecision`,
`SpawnSubtaskDecision`, `YieldForHumanDecision`, `WaitTimerDecision`,
`FinishDecision`, `FailDecision`, plus loop-continuing writes such as a state
patch and a compaction request. The Engine routes each Decision to one of
three destinations:

| Route | Decisions | What happens |
| --- | --- | --- |
| Continue | tool calls, state patch, compaction | emit the events, don't suspend, run the next step |
| Suspend | spawn subtask(s), yield for human, wait for timer | release execution and wait to be woken |
| Terminate | finish, fail | write a snapshot and a terminal event; the Task ends |

Splitting "stating a position" (Policy) from "posting to the ledger" (Engine)
is the single-writer invariant seen from the execution side: the right to
decide is open — swap in your own Policy — while the right to record stays
closed, so even a badly behaved Policy cannot corrupt ground truth.

## Boundaries the Engine keeps

The Engine knows nothing of Workers, the Dispatcher, or HTTP — it advances
one Task by one step and stops. It is deliberately small: the control flow
only routes Decisions, delegating the actual work to peripheral handlers.
Cancellation is cooperative — the Engine probes for a stop request at safe
points between composing and deciding rather than interrupting threads.

Related: [Task model](task-model.md) ·
[Wake & resume](wake-resume.md) ·
[Architecture overview](../architecture/overview.md)
