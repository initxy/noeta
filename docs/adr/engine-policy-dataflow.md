# Engine→Policy→LLMClient dataflow: Decisions carry a typed payload, StepContext is passed explicitly

## Context

The dataflow among Engine, Policy, and LLMClient inside the kernel needs a few non-overlapping contracts to nail down "who writes state, how the Policy hands its intent to the Engine, and how task_id/lease_id/trace_id are passed down." This decision, together with the single-writer rule (see `single-writer-invariant.md`), fixes three kernel dataflow contracts covering every Engine → Policy → other-component dataflow.

## Decision

The three contracts don't overlap and together cover the whole Engine → Policy → other-component dataflow: **the Engine is the state writer**, **the Decision is the Policy's typed intent payload to the Engine**, and **StepContext is the typed pass-down payload from Engine → Policy → LLMClient**.

### The Decision carries a typed payload; the single-writer Engine writes each slice

A `Decision` is the Policy's **intent payload** to the Engine: the Policy packs side effects into typed fields, and in its main dispatch the Engine translates each non-empty payload into a typed event and writes it into the corresponding slice. Every legal field currently maps to a single-writer slice-write intent: `state_patch` (writes TaskState), `assistant_message` (writes RuntimeState.messages), `ToolCallsDecision.calls`, and the `FinishDecision` / `FailDecision` / `SpawnSubtask(s)Decision` variants.

- **Decision fields grow within a boundary**: only a typed payload that "maps to some slice-write intent" may be added. **Do not** carry a payload with no corresponding single-writer slice—Policy internal state (keep it on the Policy instance), LLM diagnostic hints / cost / model name (put them in the `LLMResponseRecorded` payload), pure audit metadata (use an Observer or a new event type), or correlation ids (use the existing trace_id / causation_id).
- **The dispatch order is fixed**: `state_patch` first → then `assistant_message` → then the Decision branches. This order makes the EventLog sequence deterministic, so that fold rebuilds the same state and the content hash of snapshot/ContentStore stays stable.

### StepContext: passed explicitly along the chain

At step entry, the Engine constructs `StepContext(task_id, lease_id, trace_id)` (frozen, slots) and passes it explicitly to `Policy.decide(ctx, view)`; the Policy forwards the same ctx to `RuntimeLLMClient.complete(req, ctx)`, which uses ctx to fill the envelope of the three LLM events.

- **The Policy / RuntimeLLMClient must not obtain task_id / lease_id / trace_id by any other route**: no thread-local / ContextVar, no reverse lookup from the `LLMRequest` payload, no injection via an EventLog callback. ctx is a **read-only pass-down channel, not a service registry**: do not stuff Policy internal state, an EventLog reference, a provider client instance, or mutable config into it.
- **The `LLMProvider` Protocol does not take ctx**: a provider is pure transport, does not write the EventLog, and keeps the `complete(request) → response` shape (see `provider-neutral.md`); ctx is the business of noeta's internal wrapper `RuntimeLLMClient`.
- **A `RuntimeLLMClient` instance is per-task** (not per-step, not per-process): a new task builds a new instance (bound to this task's EventLog stream), and it is discarded when the task ends. This keeps the client always bound to the current task's event stream and keeps the ctx field set minimal; cross-step connection reuse is delegated to the process-level ContentStore.

## Rationale

- **The typed payload protects the single writer.** If the Policy wrote slices directly, RuntimeState's writer would go from "only the Engine" to "the Engine + any Policy," the single-writer invariant (only the Engine produces events) would no longer hold, and the failure surface would explode. Making the Decision the "Policy → Engine intent" gives the Policy expressiveness without breaking the single writer.
- **The Engine cannot rebuild the payload without losing information.** If the Engine synthesized the message itself from the Decision type, it wouldn't have the LLM's thinking text or the original content-block structure (that information exists only after the Policy translates the `LLMResponse`). Having the Policy supply the typed message is the only faithful way.
- **ctx is passed explicitly to keep the Policy a pure function.** ContextVar / thread-local global state brings test-isolation hell (every multi-task / parallel pytest has to clear it), makes Policy behavior depend on implicit global state, and thereby implicitly widens the Policy's input surface—so resume folding the same EventLog can no longer be trusted to re-derive the same Decisions, and static analysis can't trace ctx's flow. An explicit parameter makes "Policy input = (ctx, view)" fully visible in the signature.
- **The provider not taking ctx is boundary discipline.** A provider is a third-party adapter; handing it noeta's internal pipeline (task_id / lease_id) leaks internal context past the provider boundary. A pure-function provider + an internal wrapper that consumes ctx keeps both interface boundaries clean.

## Alternatives considered

1. **Policy writes slices directly** (emitting events via an Engine-injected EventLog). Rejected: breaks the single writer, and fold can no longer trust the Engine to be the sole event origin.
2. **The Engine constructs the payload itself rather than the Policy supplying a typed message.** Rejected: the Engine's rebuild loses the thinking text and content-block structure.
3. **Wrap another layer `PolicyOutput(decision, side_effects)`.** Rejected: both the Policy and the Engine interfaces have to change, the field split between `Decision` and `PolicyOutput` would be perpetually unclear, and "the Decision both describes intent and carries a typed payload" already holds for `state_patch`—this wrapper breaks the existing consistency.
4. **Pass ctx via ContextVar / thread-local global state.** Rejected: test-isolation hell + breaks the pure-function Policy + resume can no longer re-derive the same Decisions from the same EventLog.
5. **Build a new RuntimeLLMClient closing over ctx for every step.** Rejected: the Policy holds the llm reference at construction, so swapping the LLMClient requires either adding a setter to the Policy or rebuilding the Policy each step (violating Policy immutability at construction), and the LLMClient loses cross-step connection reuse. Per-task construction is the correct lifecycle.
6. **The Policy stuffs task_id into `LLMRequest.metadata` for the LLMClient to look up.** Rejected: metadata is a provider field, so this would expose noeta's internal context past the provider boundary; and when the field is missing, you're forced to choose between "raise" and "default," both of which are new failure modes.
7. **The Engine's main loop calls `provider.complete()` directly, bypassing the Policy.** Rejected: the ReAct loop logic moves into the Engine, the Engine has to branch by Policy type and keeps swelling (hitting the engine line budget), and the Policy loses its place as the decision hub.

## Consequences

- The protocols land in `noeta.protocols.decisions` (the `Decision` union and the currently-legal fields), `noeta.protocols.step_context` (the `StepContext` typed dataclass), and `noeta.protocols.policy` (the `Policy.decide(ctx, view)` signature).
- The engine lands in `noeta.core.engine`'s `run_one_step` dispatch (state_patch first, then assistant_message, then the branches).
- The client lands in `noeta.runtime.llm` (`RuntimeLLMClient.complete(req, ctx)`, at per-task instance granularity); the Policies in `noeta.policies` are merely ctx "porters"—they forward it and never read or write it.
- Constraints and cautions: Decision fields and StepContext fields must each hold their boundary (the former only grows slice-write intents, the latter stays read-only pass-down), and the dispatch order must not change, or the determinism of fold/resume and the stability of the content hash will be broken.
