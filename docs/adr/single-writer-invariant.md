# Single-writer invariant: each state slice has exactly one physical writer, and it is always the Engine

## Context

Noeta's core promise is that the result of `fold(events) → state` must equal the runtime state — this equation is the very foundation of "rebuilding Task state purely from the EventLog" (resume, snapshot rebuild). A Task has multiple mutable state slices, and if several components could each append events to the EventLog on their own, the writer shape fold sees would fork, and the equation would no longer hold.

## Decision

Each mutable state slice of a Task has **exactly one physical writer** (the component that actually emits the event), and the physical writer of all four slices is the **Engine**:

- `RuntimeState`: written by the Engine.
- `TaskState`: written by the Engine (on behalf of the Policy, via `Decision.state_patch`).
- `ContextState`: written by the Engine (obtained by folding `ContextPlanComposed` events — the Composer computes the plan body but does **not** write the EventLog, staying a pure function).
- `GovernanceState`: written by the Engine (folded from events).

Other components (Policy, hooks, tools, providers) **cannot append events directly**. A Policy expresses write intent via a `Decision` typed payload (`state_patch` / `assistant_message` / ...), and the Engine translates it into a typed event when it dispatches in `run_one_step`. This guarantees that the event envelope's `actor` field always equals the Engine, that fold sees a single actor shape, and that no branch for "Policy appends directly" is needed.

Derived data (subtask_results / touched_artifacts / cost_accumulated) does not go into TaskState; it is folded from the EventLog into GovernanceState or a separate view. Any new field must declare which slice it belongs to and who its physical writer is, or it doesn't pass.

## Rationale

- **This invariant is the foundation of fold/resume correctness.** The result of `fold(events) → state` must equal the runtime state; this equation is exactly what "rebuilding Task state purely from the EventLog" (resume, snapshot rebuild) depends on. A second writer that appends around the Engine would fork `fold(events) → state` from the runtime state, so the state resume rebuilds would be wrong.
- **`actor` is always the Engine, keeping the fold branching clean.** Every write goes through Engine dispatch translation, so the fold reducer sees only one writer shape and never has to case-split on "who wrote it."
- **State pollution is boxed in.** Multiple writers plus a generic `ExtensionState` dict would let any component implicitly do `run.extension["x"]=y`, bypassing the type checker and making debugging and state rebuild uncontrollable.

## Alternatives considered

1. **Multiple writers + one generic `ExtensionState` dict** (any component can read/write Task state). Rejected: state pollution, hard debugging, uncontrollable state rebuild, and `run.extension["x"]=y` bypasses the type checker.
2. **A fully immutable Task, generating a new Task each step.** Rejected: reallocating four large slices each step adds GC pressure, and both event fold and state update logic would have to be rewritten twice over.

## Consequences

- Where this bears weight: `noeta.core.engine` is the sole physical writer of the four slices, and every external "I want to change state" path converges here; `noeta.core._decision_handlers`, `noeta.core.fold` handle Decision → event translation and per-slice reducer routing; `noeta.policies.react`, `noeta.context.composer` express write intent only via a `Decision` / plan body and never append directly.
- Constraint: a hook that wants to "modify" state must instead become part of some Policy or ContextComposer, not stand on its own (the Mutator hook is deprecated, see `docs/adr/guard-observer-hooks.md`). A new field must explicitly declare its owning slice and physical writer, or it doesn't pass.
