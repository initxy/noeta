# Control tools are SDK material; the kernel carries only neutral Decisions — `StatePatchDecision` and `yield_for_human`

## Context

A coding agent's model-visible control tools — writing a todo list, asking the
user a structured question, activating a skill, delegating — carry product
semantics: a todo schema, question count and length limits, validators, a skill
menu. The kernel's job is mechanism; it must hold no opinion about what an agent
looks like. The `Decision` payload is declared opaque to the Engine
(`engine-policy-dataflow.md`), which holds only if no kernel branch reads a todo
or question shape. That leaves the question of which neutral mechanism a control
tool's effect travels on.

## Decision

**The kernel holds no control-tool schema, description, or translate body.**
Those live in the built-ins that own them; the kernel band keeps the neutral
mechanism only — the shared schema and acknowledgement helpers, the
decision-time translate context, the routing spec type, and the dispatcher that
walks it.

**`StatePatchDecision` is the persistent-state-write twin of
`ToolCallsDecision`.** It is the member of the `tool_calls` family that lets the
main loop continue: it invokes no ToolRuntime tool, does not suspend, and does
not terminate. The kernel commits, in a fixed deterministic order, the messages
the Policy built plus an optional `TaskStatePatch`:

`messages_before` → `TaskStatePatched` (only when a patch is present) →
`messages_after`

Every message and every patch field is authored by the Policy. The kernel
commits them in order and reads no todo, question, or skill shape out of them.

**`ask_user_question` routes through the neutral HITL primitive.** A valid call
becomes a `YieldForHumanDecision` carrying an opaque `HitlRequestAnchor`; the
question body spills to the ContentStore. The kernel keeps
`UserQuestionRequested` / `UserQuestionAnswered` and the
`governance.pending_questions` slice as neutral audit — an opaque `ContentRef`
plus a count and an id, structurally identical to `pending_approvals` — and
never parses the question schema. The schema, the limits (1–3 questions, ≤5
choices, header ≤40 characters), the validators and the answer codec belong to
the `ask_user_question` built-in.

**Guard verdicts stay neutral.** `VerdictResult` carries `verdict` and `reason`
only. No product convention is encoded in the reason string and no kernel branch
parses one out.

**The kernel's vocabulary stays neutral throughout.** Risk is the neutral
`low` / `medium` / `high` triple; the Claude-to-Noeta tool-name alias map that
skill frontmatter needs belongs to the skills built-in, not to the permission
guard; a worker's default provenance is the host-neutral `host`.

**The `Decision` union is "the set of neutral mechanisms," not a fixed count.**
Membership is decided by asking whether a variant is a neutral mechanism the
Engine can execute without understanding the payload — never by preserving a
number.

## Rationale

- **A kernel that knows about todos and questions breaks the mechanism
  boundary.** Baking a product schema into it also makes payload opacity a claim
  the code contradicts, which is worse than not claiming it.
- **`StatePatchDecision` rather than a real ToolRuntime tool: there is no
  external side effect.** These calls write a little durable task state and some
  conversational bookkeeping. ToolRuntime is the mechanism for executing an
  external action and recording its artifact; a tool that produces no artifact
  would need a special case carved through it.
- **`StatePatchDecision` rather than reusing `ToolCallsDecision`: the result is
  known immediately.** `ToolCallsDecision` means "dispatch an external call for
  the Engine to run," but a control tool's result *is* the patch. Reusing it
  would force either a fake round-trip or an Engine that branches on
  `tool_name` — product semantics back inside the kernel.
- **The recorded event shape is the lifeline.** `StatePatchDecision` emits the
  `MessagesAppended` and `TaskStatePatched` events; control-tool state such as
  the todo list rides optional trailing patch fields, so a recording written
  without them folds unchanged. No new event type, and resume re-emission is
  identical.
- **Asking a question needs no new concept.** `yield_for_human` is the neutral
  HITL primitive, an opaque anchor is enough to carry the request, and the audit
  slice then matches the approval slice it sits beside.

## Alternatives considered

1. **Keeping the control tools in the kernel but marking them "product-only."**
   Rejected: a label changes nothing — the kernel would import the product
   schema and have to understand todo, question and skill shapes, so neither the
   mechanism boundary nor payload opacity would hold.
2. **Making the state-writing control tools real ToolRuntime tools.** Rejected:
   no external side effect and no artifact, so ToolRuntime would need an
   artifact-less special case that is more convoluted than the neutral variant.
3. **Reusing `ToolCallsDecision` to carry them.** Rejected: it buys either a
   fake round-trip or an Engine that dispatches on tool name.
4. **Giving `ask_user_question` its own structured-question Decision variant.**
   Rejected: redundant — `yield_for_human` plus an opaque anchor carries it,
   with audit consistent with pending approvals.
5. **A model-visibility flag on `VerdictResult`, or a reserved prefix in the
   deny reason.** Rejected: both put a product convention inside the guard
   contract, one as a typed field and one as a string the kernel must parse.

## Consequences

- The neutral variant and the union live in `noeta.protocols.decisions`; the
  fixed-order commit is a decision handler in the kernel core.
- The kernel's share is `noeta.policies.control_semantics`: shared neutral
  primitives, the translate context and routing spec, the dispatcher, and the
  few recorded-wire names kernel routing cannot avoid touching.
- The product semantics — schemas, limits, validators, translate bodies,
  description text — live in the `todo_write`, `ask_user_question`,
  `delegation`, `skills` and `react` built-ins.
- Byte-safety constraint for anything built on this path: no new event type,
  reuse `MessagesAppended` and `TaskStatePatched`, and add control state as
  optional trailing fields so a recording without them folds with zero drift.
