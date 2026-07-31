# Guard vs Observer

Noeta has exactly two hook roles, split by one question: **does the hook need
to stop the action, or only to see it?**

## Guards: synchronous veto on the hot path

A Guard runs inside the Engine's step, *before* an effect happens, at three
action points — `ProposedToolCall`, `ProposedSpawnSubtask`, `ProposedFinish`.
It returns a `VerdictResult` carrying one of three verdicts (`ALLOW`, `DENY`,
`REQUIRE_APPROVAL`) plus an optional reason. Because it completes before the
effect, it can genuinely prevent it.

`REQUIRE_APPROVAL` opens no parallel lifecycle: the Engine maps it to the same
human-suspend exit a `YieldForHumanDecision` takes, so approval reuses one wake
handle and one resume branch.

The interface is small:

```python
class Guard(Protocol):
    name: str
    priority: int

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult: ...
```

`GuardContext` is read-only: `task_id`, a deepcopy of the folded
`GovernanceState`, the task's `active_skills` and `subtask_depth`, and the
recent tool-call identity keys. A Guard that mutates what it was handed cannot
perturb engine state.

`HookManager` (`noeta.core.hooks`) runs the registered Guards in ascending
`priority` and returns the **first non-allow** verdict; lower-priority Guards
are not consulted. A Guard whose `check` raises is converted into a `DENY`
naming that Guard, and that deny decides — a broken Guard cannot quietly grant
what it exists to block.

The `governance` built-in contributes the default stack:

| Priority | Guard | Enforces |
| --- | --- | --- |
| 10 | `BudgetGuard` | caps on iterations, tool calls, cost, spawned subtasks, subtask depth |
| 20 | `PermissionGuard` | tool / agent allowlists and a risk-level ceiling, fail-closed |
| 30 | `RepetitionGuard` | breaks a stuck run of identical `(tool_name, arguments)` calls |
| 100 | `HookGuard` | user-configured PreToolUse rules |

Because the first non-allow wins, a user rule at priority 100 can only tighten a
call the built-ins already allowed; it can neither loosen a built-in denial nor
rewrite a built-in approval.

## Observers: post-commit subscribers

An Observer subscribes a callback to the EventLog through
`EventLogSubscriber.subscribe`. Every adapter honours one delivery contract: the
callback fires **after** the append is durable and before the originating `emit`
returns; it fires **outside** the adapter's writer lock, so several writer
threads can enter one callback concurrently and each Observer guards its own
state; and an exception it raises is **swallowed**, so a broken Observer cannot
take a Task down with it.

In-tree Observers: `AuditObserver` (an allowlisted projection of every envelope
into a sink — never the payload bodies), `MetricsObserver` (per-type and
per-task counters), `TraceExportObserver` (that projection shipped to an
external sink), `EventFanout` (transport-neutral fan-out to bounded per-consumer
queues), and `ChildLifecycleObserver` (the parent ↔ child handoff). The
`governance` built-in adds `HookObserver` for user PostToolUse / Notification
hooks; it enqueues onto a background thread rather than running a subprocess
inside the emit path.

Observers read. The one that writes is `ChildLifecycleObserver`, and it writes
narrowly: it appends `SubtaskCompleted` to the **parent's** stream through
`system_emit` — a lease-free cross-stream append tagged `origin="observer"` —
and hands the wake to the Dispatcher. It never writes the stream whose event
triggered it, so no stream gains a second concurrent writer.

There is no third role. A hook that wants to rewrite a payload or a state slice
must become part of a Policy or the ContextComposer; the single-writer invariant
admits no second writer (see [Event sourcing](event-sourcing.md)).

## Why the split

| | Guard | Observer |
| --- | --- | --- |
| Runs | before the effect, synchronously | after the envelope is durable |
| Can veto | yes (`ALLOW` / `DENY` / `REQUIRE_APPROVAL`) | no |
| Failure impact | treated as a deny — fail-closed | swallowed; Task unaffected |
| Typical use | permissions, budget, loop-breaking | audit, metrics, tracing, fan-out |

Vetoing sits on the hot path, so the surface stays at three well-defined points.
Observation must never block or corrupt execution, so it is pushed past the
commit and denied the right to fail loudly. Collapsing the two into one
"middleware" surface would force every audit hook to be trusted like a
permission check.

Both are process-scoped wiring surfaces that never collide, so every
contribution applies. Pass your own as `Options.guards` (`Guard` instances,
registered after the built-in stack) and `Options.observers`
(`Callable[[EventEnvelope], None]`, subscribed alongside the defaults and torn
down on shutdown). Neither enters agent identity.

Related: [Engine & execution](engine-execution.md) ·
[Event sourcing](event-sourcing.md)
