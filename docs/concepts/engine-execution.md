# Engine & execution

The **Engine** is the part of Noeta that actually moves a Task forward. You
hand it a Task and it runs until the Task has to wait for something or is
finished, then it returns. It keeps nothing in memory between calls: every call
starts from a Task freshly folded out of the EventLog (see
[event sourcing](event-sourcing.md)).

The single verb is `run_one_step(task, lease_id=…)`. "One step" means *up to the
next suspend or terminal* — not one model round-trip. A step that runs ten tool
calls and three model turns is still one `run_one_step` call.

<p align="center">
  <img src="../assets/diagrams/engine-execution.svg" alt="Engine execution — compose → decide → dispatch, with tool_calls looping in place" width="820">
</p>

## The loop: compose → decide → dispatch

Inside one call the Engine repeats three phases:

1. **Compose.** The **ContextComposer** assembles the `View` — the exact input
   the model will see — out of the folded state, and the Engine records a
   `ContextPlanComposed` envelope naming what the turn was built from (see
   [composer & cache](composer-and-cache.md)). This happens once per pass
   through the loop, so a long step composes many times and each one is
   auditable afterwards.

2. **Decide.** The **Policy** reads the `View` and returns a typed `Decision`.
   The Policy is a pure function: it emits no events, touches no storage, and
   has no write access anywhere. It only states a position. `ReActPolicy` is
   the default; a deterministic stub Policy stands in for tests.

3. **Dispatch.** The Engine routes on the decision type and lands its effects —
   tool calls, LLM round-trips, subtask spawns, suspension, termination — as
   envelopes appended through the lease-validated EventLog.

A `Decision` is a small dataclass. The most common one carries the calls the
model asked for:

```python
ToolCallsDecision(
    calls=[ToolCall(tool_name="read", arguments={"path": "README.md"},
                    call_id="call_1")],
)
```

That decision keeps the loop turning: the Engine runs the tools, appends the
results, recomposes, and asks the Policy again. Only an exit decision ends the
call.

## Where one turn fits in a host

Zooming out one level, here is a whole turn as a host sees it — a goal comes
in, a Worker leases the Task, the step loop above runs, and an answer comes
back out:

<p align="center">
  <img src="../assets/diagrams/turn-sequence.svg" alt="One turn — host code → Client → Engine → Provider → Tool → EventLog, and the return path" width="820">
</p>

Everything between the lease and the release is a single `run_one_step` call.

## The Decision vocabulary

The Policy speaks a small, deliberately neutral vocabulary, and the Engine
routes each decision to one of three destinations:

| Route | Decisions | What happens |
| --- | --- | --- |
| **Continue** | `ToolCallsDecision`, `StatePatchDecision`, `CompactionRequestedDecision`, a background `SpawnSubtaskDecision` | emit the events, don't suspend, loop back to compose → decide |
| **Suspend** | a foreground `SpawnSubtaskDecision`, `SpawnSubtasksDecision`, `YieldForHumanDecision`, `WaitTimerDecision`, `WaitExternalDecision` | write a snapshot, emit `TaskSuspended`, release execution and wait to be woken |
| **Terminate** | `FinishDecision`, `FailDecision` | write a snapshot and a terminal event; the Task ends |

The vocabulary is neutral on purpose: none of these variants name a product
feature. A to-do list update is a `state_patch`, asking the user a question is
a `yield_for_human`, invoking a skill is a `state_patch`. The Engine assigns no
meaning to the payloads — the built-in that contributed the control tool does
the translating.

## Two decisions that sit on a line

- A `SpawnSubtaskDecision` with `background=True` continues the turn when a
  background launcher is wired, and suspends on a barrier otherwise.
- A `ToolCallsDecision` normally continues, but a Guard that demands approval
  turns it into a suspend on the spot (see
  [guard vs observer](guard-observer.md)). The blocked call is recorded first,
  so resume can reconstruct it exactly.

Splitting "stating a position" (the Policy) from "posting to the ledger" (the
Engine) is the single-writer invariant seen from the execution side. The right
to *decide* is an open extension point — swap in your own Policy — while the
right to *record* stays closed, so even a badly behaved Policy cannot corrupt
ground truth.

## Boundaries the Engine keeps

The Engine knows nothing about Workers, the Dispatcher, or any transport. It
advances one Task by one step and stops. Its control flow only routes
decisions; the real work lives in per-decision handlers.

Two details matter when things go wrong:

- **Cancellation is cooperative.** An optional `cancelled` predicate is polled
  at the top of each pass and again immediately after the Policy decides, so a
  cancel that lands mid-round-trip abandons the result rather than interrupting
  a thread. Its granularity is therefore the turn boundary.
- **Long steps still get resume points.** A Policy that never yields would
  otherwise leave nothing to recover from, so the Engine writes a snapshot
  every 20 consecutive tool-call turns
  (`CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD`).

The main loop itself is **locked** — it is not an extension surface. What is
open is everything around it: the Policy, the tools, the guards, the observers,
and the plugins that contribute them.

## Next

- [Wake & resume](wake-resume.md) — what happens after a suspending decision.
- [Composer & context caching](composer-and-cache.md) — what the compose phase
  actually builds.
- [Worker loop](../reference/worker-loop.md) — the component that leases Tasks
  and calls the Engine.
- [Extension planes](../architecture/extension-planes.md) — which parts around
  the loop you can replace.
