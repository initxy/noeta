# Guard vs Observer

There are exactly two ways to hook into a running agent in Noeta, and picking
between them takes one question: **does your hook need to stop the action, or
only to see it?**

If it needs to stop something — a tool call that is not allowed, a budget that
is spent — you want a **Guard**. If it only needs to know something happened —
audit, metrics, tracing, pushing updates to a UI — you want an **Observer**.
There is no third option, and that is on purpose.

## At a glance

| | Guard | Observer |
| --- | --- | --- |
| Runs | before the effect, synchronously | after the event is durably appended |
| Can veto | yes — `ALLOW` / `DENY` / `REQUIRE_APPROVAL` | no |
| If it raises | treated as a deny — fail-closed | swallowed; the Task is unaffected |
| Typical use | permissions, budget, loop-breaking | audit, metrics, tracing, fan-out |
| Scope | process-wide once loaded | process-wide once loaded |

## Guards: a synchronous veto on the hot path

A Guard runs inside the Engine's step, *before* an effect happens, at three
action points: `ProposedToolCall`, `ProposedSpawnSubtask`, `ProposedFinish`.
Because it completes before the effect, it can genuinely prevent it.

The interface is small:

```python
class Guard(Protocol):
    name: str
    priority: int

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult: ...
```

It returns a `VerdictResult` carrying one of three verdicts plus an optional
reason:

```python
VerdictResult.deny("shell_run is disabled for this agent")
```

`REQUIRE_APPROVAL` opens no parallel lifecycle. The Engine maps it to the same
human-suspend exit a `YieldForHumanDecision` takes, so approval reuses one wake
handle and one resume branch (see [wake & resume](wake-resume.md)).

### What a Guard can see

`GuardContext` is read-only: `task_id`, a deepcopy of the folded
`GovernanceState`, the Task's `active_skills` and `subtask_depth`, the recent
tool-call identity keys, and a free-form `metadata` bag. The deepcopy is the
point — a Guard that mutates what it was handed cannot perturb engine state.

### How the stack resolves

`HookManager` (`noeta.core.hooks`) runs the registered Guards in ascending
`priority` and returns the **first non-allow** verdict. Lower-priority Guards
are then not consulted at all.

A Guard whose `check` raises is converted into a `DENY` naming that Guard, and
that deny decides. A broken Guard can never quietly grant what it exists to
block.

The `governance` built-in contributes the default stack:

| Priority | Guard | Enforces |
| --- | --- | --- |
| 10 | `BudgetGuard` | caps on iterations, tool calls, cost, spawned subtasks, subtask depth |
| 20 | `PermissionGuard` | tool / agent allowlists and a risk-level ceiling, fail-closed |
| 30 | `RepetitionGuard` | breaks a stuck run of identical `(tool_name, arguments)` calls |
| 100 | `HookGuard` | user-configured PreToolUse rules |

Because the first non-allow wins, a user rule at priority 100 can only tighten
a call the built-ins already allowed. It can neither loosen a built-in denial
nor rewrite a built-in approval.

## Observers: post-commit subscribers

An Observer subscribes a callback to the EventLog through
`EventLogSubscriber.subscribe`. Every storage backend honours the same delivery
contract:

- the callback fires **after** the append is durable, and before the
  originating `emit` returns;
- it fires **outside** the backend's writer lock, so several writer threads can
  enter one callback concurrently — each Observer guards its own state with a
  lock;
- an exception it raises is **swallowed**, so a broken Observer cannot take a
  Task down with it.

The in-tree Observers are `AuditObserver` (an allowlisted projection of every
envelope into a sink — never the payload bodies), `MetricsObserver` (per-type
and per-task counters), `TraceExportObserver` (that projection shipped to an
external sink), `EventFanout` (transport-neutral fan-out to bounded per-consumer
queues), and `ChildLifecycleObserver` (the parent ↔ child handoff). The
`governance` built-in adds `HookObserver` for user PostToolUse / Notification
hooks; it enqueues onto a background thread rather than running a subprocess
inside the emit path.

### The one Observer that writes

Observers read. `ChildLifecycleObserver` is the exception, and it writes
narrowly: it appends `SubtaskCompleted` to the **parent's** stream through
`system_emit` — a lease-free cross-stream append tagged `origin="observer"` —
and hands the wake to the Dispatcher.

It never writes the stream whose event triggered it. That is what keeps the
exception safe: no stream ever gains a second concurrent writer.

## Why the split, and why no third role

Vetoing sits on the hot path, so its surface stays at three well-defined points
and its failures are loud. Observation must never block or corrupt execution,
so it is pushed past the commit and denied the right to fail loudly.

Collapsing them into one "middleware" surface would force every audit hook to
be trusted like a permission check — and would invite hooks that rewrite
payloads on the way through. A hook that wants to change *what happens* has to
become part of a Policy or the ContextComposer instead; the single-writer
invariant admits no second writer (see [event sourcing](event-sourcing.md)).

## Wiring your own

Both are process-scoped wiring surfaces that never collide, so every
contribution applies:

```python
Options(
    system_prompt="…",
    guards=(MyGuard(),),                 # registered after the built-in stack
    observers=(lambda envelope: ...,),   # subscribed alongside the defaults
)
```

Neither enters agent identity — two recipes differing only in guards or
observers compile to the same `AgentSpec`. And because governance is operator
authority rather than per-agent configuration, a loaded Guard or Observer is in
force process-wide rather than following plugin activation.

## Next

- [Engine & execution](engine-execution.md) — the step Guards run inside.
- [Write a plugin](../how-to/write-a-plugin.md) — packaging a Guard or Observer
  as a contribution.
- [Plugin surfaces](../reference/plugin-surfaces.md) — the `guard` and
  `observer` surfaces in full.
- [SDK options](../reference/sdk-options.md) — the `guards` / `observers`
  fields and the permission modes.
