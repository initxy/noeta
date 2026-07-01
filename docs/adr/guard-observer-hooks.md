# Hooks have exactly two roles: Guard and Observer

## Context

A hook mechanism is needed to let users extend governance (permission / budget / audit), but the Engine's line budget can't absorb a heavyweight hook system. This decision narrows hooks to a minimal two roles.

## Decision

Noeta's hook system has exactly two roles:

- **Guard**: synchronous, 3 action points (`before_tool_call` / `before_spawn_subtask` / `before_finish`), returning `allow` / `deny` / `require_approval`.
- **Observer**: asynchronous, subscribes to EventLog events; a failure doesn't affect the Task (at most it records a metric).

**The Mutator role is cut.** A hook that wants to "modify" payload / state must instead become part of a Policy or ContextComposer (consistent with `docs/adr/single-writer-invariant.md`).

Hook ordering uses a single integer `priority`; **no topological sort**. A lifecycle phase is not a separate mechanism—it is just an Observer subscription on ordinary events. Other constraints:

- A Guard returning `require_approval` is turned directly into `yield_for_human`. There are **no** separate `ApprovalRequested` / `ApprovalGranted` / `ApprovalRejected` event types—approval is a special case of HITL.
- observability (metrics / tracing / log / SSE / audit) is **implemented entirely by Observers**. The Engine emits no telemetry directly; the fan-out consumer is likewise an Observer, not part of fan-out itself.
- 5 built-in hooks are enabled by default: `BudgetGuard` / `PermissionGuard` / `AuditObserver` / `MetricsObserver` / `SseObserver`.
- An exception thrown by an Observer / EventLog subscriber **must never flow back to the writer**—it is always swallowed (at most a metric is recorded).

## Rationale

- **The Engine's line budget can't absorb a heavyweight hook system.** "3 roles × 8 step phases × 4 lifecycle phases + a runs_after topology + per-tool verdict" would spend 30%+ of the Engine code weaving hooks—textbook overengineering. Cutting to two roles + a single integer priority keeps the Engine body lean.
- **Approval shouldn't monopolize three events.** `require_approval → yield_for_human` reuses the same HITL suspend channel, so fold/resume doesn't grow a new event-type branch just for approval.
- **observability must be decoupled from the main loop.** Making all telemetry Observers keeps the Engine main loop free of any metric / SSE / audit, so the main path's determinism isn't polluted by observation side effects. An Observer failure records at most a metric and never flows back to the writer—preventing "one blown-up SSE subscriber dragging down the EventLog writer."

## Alternatives considered

1. **3 roles × 8 step phases × 4 lifecycle phases + a runs_after topology + per-tool verdict.** Rejected: expressive, but it spends 30%+ of the Engine code weaving hooks and blows the budget—and at the time no real business hook needed it. Textbook overengineering.
2. **No hooks, hard-code governance into the Engine.** Rejected: users can't extend it, and every audit / permission / budget change requires touching the Engine.

## Consequences

- The Guard's 3 action points + the verdict types themselves land in `noeta.core.hooks`, `noeta.protocols.hooks`, and `noeta.guards.*`.
- The Observer's async subscription (swallowing exceptions, not flowing back to the writer) lands in `noeta.observers.*` (`audit` / `fanout` / `__init__`) and `noeta.guards.hook`.
- The `require_approval → yield_for_human` conversion lands in `noeta.core._decision_handlers`.
- The swallowing of EventLog-subscriber exceptions lands in `noeta.storage.sqlite.eventlog`.
- A content-rewriting need cannot go through a hook; it must move to a Policy or ContextComposer, to preserve the single-writer invariant.
