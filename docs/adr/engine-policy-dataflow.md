# Engine → Policy → LLMClient dataflow: a Decision carries a typed payload, StepContext is passed explicitly

## Context

Three questions inside the kernel need non-overlapping answers: who writes state, how a Policy hands its intent to the Engine, and how `task_id` / `lease_id` / `trace_id` reach the code that records LLM round-trips. Together with the single-writer rule (`single-writer-invariant.md`), the contracts below cover the whole Engine → Policy → downstream dataflow.

## Decision

The Engine is the state writer. The Decision is the Policy's typed intent payload to the Engine. StepContext is the typed pass-down payload from Engine → Policy → LLMClient.

### The Decision carries a typed payload; the Engine writes each slice

A `Decision` is the Policy's intent payload: the Policy packs side effects into typed fields, and the Engine's dispatch translates each non-empty payload into a typed event written into the matching slice. Every legal field maps to exactly one single-writer slice-write intent — `state_patch` (TaskState), `assistant_message` (RuntimeState messages), `ToolCallsDecision.calls`, and the finish / fail / spawn-subtask(s) / yield / wait variants.

- **Decision fields grow within a boundary.** Only a typed payload that maps to a slice-write intent belongs on a Decision. Everything else has a home elsewhere: Policy internal state on the Policy instance, LLM diagnostics / cost / model name in the `LLMResponseRecorded` payload, audit metadata in an Observer or a new event type, correlation ids in `trace_id` / `causation_id`. A tag that names a field of an event the handler emits regardless — `YieldForHumanDecision.suspend_reason`, `request_anchor` — sits inside the boundary: nothing branches on the tag, so producers may add values without a protocol bump.
- **The dispatch order is fixed:** `state_patch`, then `assistant_message`, then the Decision branch. The order makes the event sequence deterministic, so fold rebuilds the same state and a snapshot's content hash stays stable. The two loop-continuing decisions that own ordered emits of their own — a state-write control tool (messages before → patch → messages after) and a compaction request (tag → requested → compacted) — run their handler directly rather than passing through the generic pre-apply.
- **A non-final conversational turn does not terminate on failure.** `TaskFailed` is terminal: it seals the ledger, spending a whole conversation and everything the person built up in it on one transient fault. The multi-turn Policy wrapper translates a `FailDecision` on a non-final turn into the same next-goal suspend an ordinary turn rests on, tagged `turn_failed: <reason>`. A `final=True` turn does terminate — there is no next human turn to park for.

### StepContext is passed explicitly along the chain

Each turn the Engine builds a frozen `StepContext` and passes it to `Policy.decide(ctx, view)`; the Policy forwards the same ctx to `RuntimeLLMClient.complete(req, ctx)`, which uses it to fill the envelope of the three LLM events. It carries the three step identifiers, the input-token count the provider reported for the previous round-trip (a compaction-aware Policy uses it as a deterministic history baseline instead of estimating the whole prompt with a character heuristic), and an Engine-supplied applier callback the client invokes after each of its own emits.

- **The Policy and the client obtain those identifiers by no other route:** no thread-local or ContextVar, no reverse lookup out of the `LLMRequest` payload, no injection through an EventLog callback.
- **ctx is a read-only pass-down channel, not a service registry.** Policy internal state, an EventLog reference, a provider client instance, and mutable config stay out. The applier callback is bounded by the very invariant it protects: the Engine owns the Task and supplies the applier, so the Engine is the sole physical writer of RuntimeState and the client only notifies. Without it, the client's own appends never reach the in-memory Task and `fold(events)` diverges from live state mid tool-loop — the equation the single-writer invariant rests on.
- **The `LLMProvider` Protocol does not take ctx.** A provider is pure transport, writes no events, and keeps the `complete(request) → response` shape (`provider-neutral.md`). A gateway that keys tracing off request-scoped headers is served by a host-supplied `ctx → headers` mapping applied through an optional header-aware capability, so ctx itself never crosses the provider boundary.
- **A `RuntimeLLMClient` instance is per-task** — not per-step, not per-process. It binds to that task's event stream at construction and is discarded when the task ends, which keeps the ctx field set minimal. Cross-step connection reuse belongs to the process-level provider adapter and ContentStore.

## Rationale

- **The typed payload protects the single writer.** If the Policy wrote slices directly, RuntimeState's writer would be "the Engine plus any Policy", the invariant that only the Engine produces events would fall, and the failure surface would explode. Routing intent through the Decision gives the Policy full expressiveness at no cost to the single writer.
- **Explicit ctx keeps the Policy a pure function.** Global state brings test-isolation hell (every multi-task or parallel run has to clear it), makes Policy behaviour depend on invisible inputs, and breaks the promise that folding the same event stream re-derives the same Decisions. An explicit parameter makes "Policy input = (ctx, view)" visible in the signature and traceable by static analysis.
- **Keeping ctx out of the provider is boundary discipline.** A provider is a third-party adapter; handing it internal pipeline identifiers leaks context past the adapter boundary. A pure-function provider plus an internal wrapper that consumes ctx keeps both interfaces clean.

## Alternatives considered

1. **The Policy writes slices directly**, emitting through an Engine-injected EventLog. Rejected: it breaks the single writer, and fold loses its one guarantee that the Engine is the sole event origin.
2. **The Engine constructs the assistant message itself** instead of the Policy supplying a typed one. Rejected: the rebuild loses the thinking text and the original content-block structure — information that exists only after the Policy translates the `LLMResponse`.
3. **Wrap another layer, `PolicyOutput(decision, side_effects)`.** Rejected: both the Policy and the Engine interfaces would have to change, the field split between the two objects would be perpetually unclear, and the Decision already both describes intent and carries a typed payload for `state_patch`.
4. **Pass ctx via ContextVar or thread-local state.** Rejected: test-isolation hell, an impure Policy, and a resume that fails to re-derive the same Decisions from the same stream.
5. **Build a new `RuntimeLLMClient` closing over ctx for every step.** Rejected: the Policy holds the client reference from construction, so swapping it per step needs either a setter on the Policy or a rebuilt Policy each step, and the client loses cross-step connection reuse.
6. **Stuff `task_id` into `LLMRequest.metadata` for the client to read back.** Rejected: metadata is a provider field, so this exposes internal context past the provider boundary; and a missing field forces a choice between raising and defaulting, both new failure modes.
7. **Let the Engine's main loop call the provider directly, bypassing the Policy.** Rejected: the ReAct loop logic moves into the Engine, which then branches by Policy type and keeps swelling, and the Policy loses its place as the decision hub.
8. **Gate the failed-turn substitution on `retryable`.** Rejected: the flag answers "would re-driving this same step help?", a question about the terminal path, and it is False for precisely the failures a human can clear by rephrasing — a transient provider error, an empty response, an exhausted step budget, a spent structured-output nudge budget. Gating on it would terminate the case the substitution exists for. Once a turn is parked, the next input is a new turn rather than a re-drive, so the flag has no consumer on this path; its reason text rides the suspend reason.

## Consequences

- The protocols live in `noeta.protocols.decisions` (the `Decision` union and its legal fields), `noeta.protocols.step_context` (the frozen `StepContext`), and `noeta.protocols.policy` (the `decide(ctx, view)` signature). The dispatch lives in `noeta.core.engine`, the recording client in `noeta.runtime.llm`, the turn-wrapping Policy in `noeta.execution.multi_turn`. Policies are ctx porters — they forward it and never read or write it.
- Decision fields and StepContext fields must each hold their boundary, and the dispatch order must not change, or the determinism of fold and resume and the stability of content hashes go with it.
