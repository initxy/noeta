# Single-writer invariant: a Task's state slices are mutated in exactly one place, and every change reaches them as a folded event

## Context

`fold(events) → state` must equal the live runtime state. That equation is what makes resume and snapshot rebuild possible at all. A Task carries four mutable slices — `RuntimeState`, `TaskState`, `ContextState`, `GovernanceState` — and if more than one code path could assign to a slice, the live state and the folded state would drift apart, with the drift surfacing only at resume, far from whatever caused it.

## Decision

**No component assigns to a state slice.** The only code that mutates the four slices is the reducer set in `noeta.core.fold`, and the live path re-enters those same reducers through `apply_event` right after a write. A snapshot taken mid-step therefore captures exactly what a fold over the same prefix rebuilds.

**State changes reach the log through the Engine.** Policies, guards, tools, composers and providers cannot append a state change of their own. A Policy expresses write intent as a typed `Decision` — a `TaskStatePatch`, an assistant message — and the Engine translates it into a typed event when it dispatches the step. A Guard returns a `VerdictResult` and is forbidden from mutating either of its arguments. The Composer computes the context-plan body and writes it into the ContentStore, but the ref reaches `ContextState` only because the Engine records `ContextPlanComposed` and fold reduces it; the Composer never touches the slice.

Slice ownership follows from that: `RuntimeState` is Engine-owned; `TaskState` is Policy-authored through `Decision.state_patch`, the one shape allowed to mutate it; `ContextState` is Composer-derived; `GovernanceState` is entirely fold-accumulated. Derived data — subtask outcomes, cost and per-token accounting, the background-job and background-subagent ledgers — never enters `TaskState`; it accumulates in `GovernanceState` from the stream. Any new field declares which slice it belongs to and which event fills it.

**Subsystems that record their own lifecycle do so as events, never as state writes.** The LLM client, the tool runtime, compaction, the background-shell and background-subagent runners, the child-lifecycle observer and the plugin content recorder each append events; every envelope carries an `actor` and an `origin` naming the subsystem that produced it. Those two fields are provenance for the audit projection — fold dispatches on the event type and never reads `actor`, so no attribution string can influence rebuilt state. A plugin's `init` hook holds a kernel-owned recorder rather than an EventLog: the recorder emits on its behalf, stamping the plugin's name as the actor.

## Rationale

- **This is what makes fold and resume correct.** A second path that assigned to a slice, or appended around the Engine, would fork `fold(events) → state` from the live state, and every rebuild after that point would be wrong.
- **One mutation site keeps live and replay honest by construction.** Sharing a single reducer set means there is no second code path to keep in step, and no class of bug where a live step writes a field the reducer forgets — or vice versa.
- **A typed patch boxes in state pollution.** Free-form access — a generic extension dictionary any component could write into — bypasses the type checker and makes both debugging and rebuild uncontrollable. One patch shape with one apply method keeps the mutation surface enumerable.

## Alternatives considered

1. **Multiple writers plus a generic extension dictionary on the Task**, readable and writable by any component. Weighed and rejected: state pollution, unreadable debugging, uncontrollable rebuild, and an untyped write path the checker cannot see.
2. **A fully immutable Task, rebuilt fresh each step.** Rejected: reallocating four large slices per step adds GC pressure for no correctness gain, and both the fold reducers and the update path would have to carry the same logic twice.
3. **Enforcing the invariant as a literal attribution rule** — requiring every recorded envelope to claim the Engine as its actor. Rejected: it would launder an LLM, compaction or plugin recording as Engine-authored, destroying the audit trail's only useful signal, and it buys nothing, because fold never reads the field. The property that matters is that nothing else mutates a slice.

## Consequences

- `noeta.core.fold` is the sole mutation site for the four slices; `noeta.core.engine` and `noeta.core._decision_handlers` carry the Decision-to-event translation and the per-slice reducer routing; `noeta.context.composer` and the ReAct policy express intent only, and never append.
- A hook has no route to change state, by design. Only two hook roles exist — Guard, a synchronous verdict, and Observer, an EventLog subscriber — and neither can write; logic that needs to mutate belongs to a Policy or the Composer.
- Every new state field must name its slice and the event that fills it, or it has no writer.
