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

- The Guard's 3 action points + the verdict types themselves land in `noeta.core.hooks`, `noeta.protocols.hooks`, and the built-in guard implementations in `noeta.builtins.governance.impl` (`budget` / `permission` / `repetition` / `hook_guard`; their config vocabulary sits kernel-side in `noeta.runtime.governance`).
- The Observer's async subscription (swallowing exceptions, not flowing back to the writer) lands in `noeta.observers.*` (`audit` / `fanout` / `__init__`) and the `HookObserver` in `noeta.builtins.governance.impl.hook_observer`.
- The `require_approval → yield_for_human` conversion lands in `noeta.core._decision_handlers`.
- The swallowing of EventLog-subscriber exceptions lands in `noeta.builtins.storage.impl.sqlite.eventlog`.
- A content-rewriting need cannot go through a hook; it must move to a Policy or ContextComposer, to preserve the single-writer invariant.

## Addendum (2026-07-28): plugin-contributed guards/observers are process-scoped

The manifest-plugin redesign (`plugin-contribution-bundles.md`, decision D6)
opens `guard` and `observer` as two of the standard extension surfaces, so a
third-party package can now contribute a Guard or an Observer through its
manifest. This addendum records the **effect-domain** rule for those two
surfaces, which is deliberately different from every other plugin surface.

Most plugin surfaces (`tool`, `agent`, `prompt_fragment`, `policy`,
`reminder_provider`, `reminder`, `tool_result_transform`, `content_kind`)
**follow per-agent activation**: an agent carries a contribution only if its
`Options.plugins` / `AgentDefinition.plugins` list activates the contributing
plugin. Guards and Observers do **not**:

- **A loaded `guard` / `observer` is in force for every agent in the process,
  regardless of which plugins that agent activated.** Governance is operator
  authority — permission interception, budget enforcement, and audit are not an
  agent author's choice, and an author must not be able to opt out of compliance
  by omitting an activation. This is why the two surfaces sit on the `wiring`
  plane with `activation_scope = "process"` rather than `identity` /
  `per-agent`.
- Mechanically, `PluginSet.process_hooks()` resolves every loaded external
  plugin's guard + observer values (built-in governance guards are the engine's
  own default stack, wired separately), and the `Client` folds them into the
  process-wide guard stack and the Observer subscriptions — the same stacks the
  five built-in default hooks and any `Options.guards` / `Options.observers`
  already occupy. The two-roles-only rule above is untouched: a plugin
  contributes an existing role, it does not introduce a third.

The asymmetry is the one deliberate exception in the plugin effect model; the
full table lives in `plugin-contribution-bundles.md` (D6).
