# Guard vs Observer

Noeta has exactly two hook surfaces, split by one question: **does the hook
need to stop the action, or only to see it?**

## Guards: synchronous veto on the hot path

A Guard runs inside the Engine's step, *before* an effect happens, at three
points: before a tool call, before a subtask spawn, and before finish. Its
verdict is `allow`, `deny`, or `require_approval`. Because the Guard completes
before the effect, it can genuinely prevent it — deny a shell command, block a
write outside the workspace, or force a budget-exhaustion failure.

Two Guards ship in-tree:

- **`BudgetGuard`** — enforces a Task's resource ceilings (iterations, cost,
  wall time, tool calls).
- **`PermissionGuard`** — implements the permission model behind
  `permission_mode` (whether a high-risk tool must ask before running).

## Observers: read-only subscribers after the fact

An Observer subscribes to the EventLog via `subscribe(callback)`. Callbacks
run *after* each envelope is durable — on the writer thread but outside the
writer lock — and are strictly read-only: an Observer cannot write events, so
the single-writer invariant holds (see
[Event sourcing](event-sourcing.md)). An Observer exception is swallowed; a
broken Observer can never take the Task down with it.

In-tree Observers: `AuditObserver`, `MetricsObserver`, `EventFanout` (the SSE
stream behind the web UI), and `ChildLifecycleObserver`.

## Why the split

| | Guard | Observer |
| --- | --- | --- |
| Runs | before the effect, synchronously | after the envelope is durable |
| Can veto | yes (`allow` / `deny` / `require_approval`) | no — read-only |
| Can write state | no | no |
| Failure impact | a deny is a recorded outcome | exception swallowed; Task unaffected |
| Typical use | permissions, budget | audit, metrics, live streaming |

Vetoing has to be synchronous and rare — it sits on the hot path, so the
surface is kept to three well-defined points. Observation must never block or
corrupt execution — so it is pushed after the commit and stripped of write
access. Collapsing the two into one "middleware" surface would force every
audit hook to be trusted like a permission check; keeping them apart means
extending one can't weaken the other.

Both surfaces are open extension points: pass your own `guards` and
`observers` through `Options` (see the
[architecture overview](../architecture/overview.md) for the full extension
surface).

Related: [Engine & execution](engine-execution.md) ·
[Event sourcing](event-sourcing.md)
