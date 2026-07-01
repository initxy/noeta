# Control tools are sdk material, wired in through a neutral-mechanism seam: StatePatchDecision + reusing yield_for_human

## Context

Three Claude-Code-style control tools—todo_write / plan_mode / ask_user_question—originally lived in the kernel. What they carry is **product semantics** (todo schema, plan-mode enter/exit, question count limits and validation), not "host mechanism." By the mechanism-vs-material split, they belong to the sdk. This decision demotes them from the kernel to the sdk and adds a neutral-mechanism variant as the landing seam.

The mechanism boundary follows `library-sdk-architecture.md`; the opacity of Decision payloads to the Engine follows `engine-policy-dataflow.md`.

## Decision

### Remove the three control-tool variants from the L0 closed union

Delete `TodoWriteDecision` / `PlanModeDecision` / `AskUserQuestionDecision` from `protocols/decisions.py`. They carry **product semantics**, not "host mechanism," so by mechanism vs material they belong to the sdk. After the demotion, the kernel (`noeta.core` / `noeta.protocols` / `noeta.guards`) **holds no product semantics at all**.

### Introduce a neutral variant `StatePatchDecision`: the "persistent-state-write twin" of `ToolCallsDecision`

It is the state-write member of the `tool_calls` family that **lets the main loop continue**: it neither calls a ToolRuntime tool, nor suspends, nor terminates. In a **fixed, deterministic order**, the kernel commits the messages the caller (Policy) constructed, plus an optional `TaskStatePatch`, and then continues the loop:

```text
messages_before  →  TaskStatePatched (only when there is a patch)  →  messages_after
```

**The Engine knows nothing about the payload** (see `engine-policy-dataflow.md`): every message and every patch field is constructed by the Policy, and the kernel only commits them in a fixed order and never reads any todo/plan/question shape. todo_write and plan_mode (enter/exit) are now both emitted by the sdk's `ReActPolicy` as a `StatePatchDecision` (`set_todos` / `set_phase` / `next_action` / plan-mode-exit record)—all written by the SDK author; the kernel does not know that these fields "are todo or plan."

### `ask_user_question` is routed through the existing neutral HITL primitive `yield_for_human`

Asking a question is no longer its own Decision variant; the sdk expresses it as a `YieldForHumanDecision` carrying a `HitlRequestAnchor`. The kernel keeps `UserQuestionRequested` / `UserQuestionAnswered` + `governance.pending_questions` as **neutral HITL audit**—storing only an opaque `ContentRef` + count + id, **structurally identical to `pending_approvals`**, and never parsing the question schema. The question schema, UI limits (≤3 questions / ≤5 options / header ≤40 characters), validators, and codec all move into the sdk (`policies/control_tools.py`); `protocols/user_questions.py` is deleted.

### Clean the leftover product flavor from the kernel

- **Typed plan-mode deny**: originally a `model_visible_on_deny: bool` was added to `VerdictResult` to let the kernel decide whether a deny is model-visible, **replacing** the practice of guessing a product-convention prefix via `reason.startswith('plan_mode_read_only:')`. **Current state**: the plan-mode path was later removed (see `workspace-and-session-path.md`), so both `VerdictResult.model_visible_on_deny` and the `plan_mode_read_only:` prefix no longer exist; `VerdictResult` now carries only `verdict` + `reason`.

- `guards/permission.py` drops the `_CLAUDE_TO_NOETA_TOOL` alias table (moved into the sdk's `policies/skill_tools.py`); risk is unified to the neutral `low/medium/high`.

- The stale `'cli'` provenance default in `runtime/worker.py` is changed to the host-neutral `'host'`.

### The `Decision` union = "the set of neutral-mechanism variants," no longer a fixed count

The accurate description: the union = **7 canonical neutral variants** + `SpawnSubtasksDecision` (fan-out, see `subtask-fanout-and-durable-wake.md`) + `StatePatchDecision` (the persistent-state write that lets the loop continue, this decision). The criterion shifts from "count the number" to "is it a neutral mechanism," loosening the old "only 7 Decisions" wording in `task-as-only-primitive.md`.

## Rationale

- **Control tools are demoted out of the kernel because the kernel knowing about todo/plan/question already violates the mechanism boundary.** The kernel must have zero opinion about "what an agent looks like"; baking a product schema into it breaks mechanism vs material and also turns "the Decision payload is opaque to the Engine" into a lie in practice.

- **Choosing `StatePatchDecision` over a real ToolRuntime tool: todo/plan have no external side effect.** They are essentially "write a bit of persistent task state + record a bit of conversational bookkeeping." Making them a `Tool` would bypass ToolRuntime's execute/record machinery (ToolRuntime is the mechanism for "executing an external action") and impose special-case semantics for a tool that produces no artifact and only mutates kernel state.

- **Choosing `StatePatchDecision` over reusing `ToolCallsDecision`: a control tool's result is known to the Policy right away.** `ToolCallsDecision` means "dispatch an external call for the Engine to run," but a control tool's result is the patch itself. Reusing it would either impose a fake round-trip or make the Engine treat tools differently by tool_name—leaking product semantics back into the kernel.

- **Keeping the recorded event shape byte-safe is the lifeline.** `StatePatchDecision` still produces the existing `MessagesAppended` + `TaskStatePatched`; `set_todos` is an optional trailing field, so an old recording without that key still folds to `None` unchanged (no new event type, and resume re-emission is fully identical).

- **ask reuses `yield_for_human`, with zero new concepts.** It is already the neutral HITL primitive; an opaque `HitlRequestAnchor` is enough to carry it, and the audit is consistent with `pending_approvals`.

## Alternatives considered

1. **Keep the control tools in the kernel but "mark" them as product-only.** Rejected: it treats the symptom, not the cause—the kernel still imports the product schema and still has to understand todo/plan/question, so neither the mechanism boundary nor payload opacity holds.

2. **Make todo/plan real ToolRuntime tools.** Rejected: no external side effect, and the result is known to the Policy right away; forcing them into ToolRuntime would require inventing an artifact-less special case, which is more convoluted.

3. **Reuse `ToolCallsDecision` to run the control tools.** Rejected: either a fake round-trip, or the Engine treats tools differently by tool_name → product semantics leak back into the kernel.

4. **Give ask_user_question its own neutral "structured question" Decision variant.** Rejected: redundant—`yield_for_human` + `HitlRequestAnchor` already carry it, with audit consistent with `pending_approvals`.

## Consequences

- The neutral variant lands in: the `StatePatchDecision` variant + the union description in `noeta.protocols.decisions`, and the fixed-order committing `handle_state_patch` in `noeta.core._decision_handlers`.

- The product semantics migrated into the sdk land in: the todo/plan/question schema/limits/validation in `noeta.policies.control_tools` / `noeta.policies.control_semantics`, the migrated tool alias table in `noeta.policies.skill_tools`, and the policy that emits `StatePatchDecision` in `noeta.policies.react`.

- The host-neutral provenance default `'host'` is in `noeta.runtime.worker`.

- The plan-mode path was removed along with `workspace-and-session-path.md`, taking with it the `VerdictResult.model_visible_on_deny` field and the `plan_mode_read_only:` prefix; `VerdictResult` now has only `verdict` + `reason`, and neither of those two should be referenced anymore.

- Byte-safety constraint: the control-tool migration introduced no new event type and still reuses `MessagesAppended` + `TaskStatePatched`, with `set_todos` and the like existing as optional trailing fields, guaranteeing zero-drift folding of old recordings—future changes to this path must continue to hold this line.
