# Hooks have exactly two roles: Guard and Observer

## Context

Governance — permission, budget, audit — must be extensible without the Engine
absorbing a hook framework. The execution core is held to a tight line budget,
and every line spent weaving hook plumbing is a line not spent on the step loop.

## Decision

**Guard** — synchronous, three action points (`before_tool_call`,
`before_spawn_subtask`, `before_finish`), three verdicts (`allow`, `deny`,
`require_approval`). Ordering is a single integer `priority`, ascending; the
first non-allow verdict decides and lower-priority Guards are not consulted. A
Guard whose check raises is treated as a deny with a synthetic reason and counts
as that deciding verdict, so a broken Guard cannot quietly grant what it exists
to block.

**Observer** — asynchronous, subscribing to the EventLog post-commit and outside
the writer lock. An Observer failure never reaches the writer; it is swallowed,
at most recording a metric. Callbacks can run concurrently from several writer
threads, so each Observer guards its own state.

**There is no Mutator role.** A hook that wants to rewrite a payload or state
must become part of a Policy or a ContextComposer instead — the single-writer
invariant (`single-writer-invariant.md`) admits no second writer.

A lifecycle phase is not a separate mechanism: it is an Observer subscription on
ordinary events. Observability — metrics, tracing, audit, transport fan-out —
is implemented entirely by Observers; the Engine emits no telemetry itself.

`require_approval` converts into the same human-suspend exit a `yield_for_human`
decision takes — one wake handle, one suspend reason, one resume branch. It
carries durable payloads recording which call is blocked and how a human
resolved it, because a resumed process must reconstruct the exact pending call
from the log, but it opens no parallel suspend/wake lifecycle.

The default stack is small: a budget Guard then a permission Guard always; a
repetition Guard and a rule-driven pre-tool-use Guard only when the operator
configures them, registered after the built-ins so a user rule can tighten a
call the built-ins allowed but never loosen a built-in denial. Child-lifecycle
observation is the one Observer wired by default; audit, metrics and trace
export are opt-in.

Plugin-contributed `guard` and `observer` contributions are **process-scoped**,
the one exception among extension surfaces: a loaded Guard or Observer is in
force for every Agent in the process regardless of which plugins that Agent
activated. That set is derived from the surface registry — any wiring surface
scoped to the process is governance authority by definition — and resolves into
exactly two buckets, because a Guard and an Observer are wired into two
different runtime seams. A third process-scoped surface has nowhere to go and is
refused loudly at build time.

## Rationale

The Engine's line budget cannot absorb a heavyweight hook system. Three roles
crossed with per-step and per-lifecycle phases, plus a `runs_after` topology and
per-tool verdicts, would spend a large fraction of the core on hook weaving. Two
roles and one integer priority keep the core body about the step loop.

Approval reusing the human-suspend channel means fold and resume grow no
approval-specific branch.

Observability must be decoupled from the main loop. Keeping all telemetry in
Observers keeps the step path free of metric, audit and transport side effects,
so determinism is not polluted by observation — and one blown-up subscriber
cannot drag down the EventLog writer.

Governance is operator authority. A Guard that applied only once an agent author
opted in would not be authority at all, which is why the two surfaces are the
one deliberate asymmetry in the plugin effect model.

## Alternatives considered

1. **Three roles across step and lifecycle phases, with a `runs_after`
   topology and per-tool verdicts.** Weighed and rejected: expressive, but it
   spends a large share of the execution core on hook plumbing and no real
   governance need requires it.
2. **No hooks; hard-code governance into the Engine.** Rejected: nothing is
   extensible, and every audit, permission or budget change means editing the
   core.
3. **Give approval its own request/grant/reject event lifecycle.** Rejected:
   approval is a special case of human-in-the-loop suspend, and a parallel
   lifecycle would duplicate the fold and resume branches the suspend path
   already provides.
4. **Let plugin guards and observers follow per-agent activation like every
   other surface.** Rejected: it would let an agent author disable compliance by
   simply not activating the plugin.
5. **Absorb any further process-scoped surface into the guard bucket by
   default.** Rejected: it hands the engine a value that is not a Guard and
   converts a build-time configuration error into a runtime crash.

## Consequences

- The action points, verdicts and priority loop live in `noeta.core.hooks` and
  `noeta.protocols.hooks`; the built-in guards and the hook-driven Observer in
  the `governance` built-in, with their configuration vocabulary kernel-side in
  `noeta.runtime.governance` so the kernel builder and the SDK host both speak it.
- Observer subscription, audit projection and envelope fan-out live in
  `noeta.observers`; swallowing subscriber exceptions is a backend obligation of
  the durable event-log implementations.
- The approval conversion lives in the Engine's decision handlers.
- A content-rewriting need cannot be met by a hook at all; it must move into a
  Policy or a ContextComposer.
