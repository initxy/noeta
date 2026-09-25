# The engine

The **Engine** moves one Task forward. Its single verb, `run_one_step`, runs the
Task until it has to wait or is finished — a whole turn, however many model calls
and tool calls that takes — and then returns. It keeps nothing in memory between
calls; every call starts from state freshly folded from the log.

<NtEngineLoop />

## What it guarantees

- **Deciding and recording are separate.** The policy only returns a decision and
  never writes to the log; the Engine records every effect. A misbehaving policy
  cannot corrupt the record.
- **Guards can veto; observers cannot.** A guard runs before an action and can
  block it. An observer sees events only after they are durable and cannot change
  anything.
- **A failing guard denies.** If a guard raises, the call is denied. A failing
  observer is ignored and the Task carries on.
- **Long steps still leave resume points.** A snapshot is written every 20
  consecutive tool-call turns.

## The loop

1. **Compose.** The context composer builds the `View` — the exact prompt, tool
   schemas and messages the model will see — and the Engine records a
   `ContextPlanComposed` event. See [Context and caching](context.md).
2. **Decide.** The **policy** reads the `View` and returns a typed `Decision`.
   The default is `ReActPolicy`, which asks the model.
3. **Dispatch.** The Engine carries the decision out — run tools, spawn subtasks,
   suspend, finish — and appends each effect to the log under the worker's lease.

Every *continue* decision below — not only tool calls — loops straight back to
compose. Only a suspend or terminate decision ends the call. A `Client` turn
normally ends in a suspend: the Task waits for your next message. Only `query()`
(or `multi_turn=False`) finishes the Task with `TaskCompleted`.

| Route | Decisions | Effect |
| --- | --- | --- |
| Continue | `ToolCallsDecision`, `StatePatchDecision`, `CompactionRequestedDecision`, a background `SpawnSubtaskDecision` | record events, loop again |
| Suspend | foreground `SpawnSubtaskDecision`, `SpawnSubtasksDecision`, `YieldForHumanDecision`, `WaitTimerDecision`, `WaitExternalDecision` | snapshot, `TaskSuspended`, release |
| Terminate | `FinishDecision`, `FailDecision` | snapshot, terminal event |

The vocabulary names no product feature: updating a to-do list is a state patch,
asking the user is `YieldForHumanDecision`. The built-in that contributed the tool
does the translating.

## One turn from the host's side

Your code hands a goal to a worker, which takes the lease and folds the log.
Everything between the lease and the release is one `run_one_step` call.
Cancellation is cooperative: the Engine checks for a cancel at the top of each
pass and right after the policy decides, so a cancel takes effect at the next
turn boundary.

## Guards and observers

| | Guard | Observer |
| --- | --- | --- |
| Runs | before the action, synchronously | after the event is durably appended |
| Can block | yes: `allow`, `deny` or `require_approval` | no |
| If it raises | the action is denied | the error is swallowed |
| Use for | permissions, budgets, breaking loops | audit, metrics, tracing, streaming to a UI |

Guards see three kinds of action: `ProposedToolCall`, `ProposedSpawnSubtask`,
`ProposedFinish`. They run in ascending `priority` and the **first non-allow
verdict wins**, so a later guard can only tighten what an earlier one allowed.
The `governance` built-in installs:

| Priority | Guard | Enforces |
| --- | --- | --- |
| 10 | `BudgetGuard` | iteration, tool-call, cost, subtask and depth caps |
| 20 | `PermissionGuard` | tool and agent allowlists, risk ceiling |
| 30 | `RepetitionGuard` | stops a run of identical tool calls |
| 100 | `HookGuard` | your PreToolUse rules |

`require_approval` suspends the Task exactly like asking a human a question, so
approval reuses the normal wake path.

Observers are called after each append commits, outside the writer lock, possibly
from several threads at once — guard your own state. The one observer that writes,
`ChildLifecycleObserver`, only appends a `SubtaskCompleted` to the *parent's*
stream; no stream ever gets a second writer.

## What this means for you

- To change *what the agent decides*, replace the policy. To *block* an action,
  write a guard. To *watch*, write an observer. There is no other kind of hook.
- Wire your own with `Options(guards=(MyGuard(),), observers=(fn,))`; neither
  changes agent identity. A guard or observer contributed by a loaded plugin
  applies to every agent in the process — governance cannot be opted out of.
- The Engine's main loop itself is not extensible; everything around it is.

Design records:
[guard and observer hooks](https://github.com/initxy/noeta/blob/main/docs/adr/guard-observer-hooks.md) ·
[engine per turn](https://github.com/initxy/noeta/blob/main/docs/adr/engine-per-turn.md)

## Next

- [Context and caching](context.md) — what the compose phase builds.
- [Tasks and waking](tasks-and-waking.md) — what happens after a suspend.
- [Write a plugin](../guides/plugins.md) — package a guard or observer.
