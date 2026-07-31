# Control tools are plugin contributions on the `control_tool` surface; `AgentSpec` identity is the activation tuple

## Context

Every official capability is a built-in plugin contributing through a standard
surface, and the kernel builder runs those contributions generically. Control
tools have two properties that make them awkward: their translate step produces
a Decision the kernel executes, and their schemas are functions of session state
rather than static dicts — the skill menu, the spawn directory and the
per-helper structured-output schema all exist only *after* tool assembly. They
also need gating, which is the same question as "what is an agent's identity."

## Decision

### `control_tool` is a standard extension surface

Plane **identity**, `activation_scope` **per-agent**, `collision_key` **name**,
ordering **priority**, and `activation_binding` **elsewhere** — it enters
durable identity but is projected per agent through
`PluginSet.activation_control_tools` rather than through `PluginActivation`. A
contribution is a factory `(ControlToolBuildContext) -> ControlToolMount | None`,
resolved by the loader like any other surface and run by the kernel builder's
**post-tools** mount loop. The surface is **open** to third-party plugins: a
translate returns neutral Decisions, the same trust class as the `policy`
surface.

`ControlToolBuildContext` is frozen and carries the effective
`capability_flags` — agent activation AND host kill-switch — with a `flag(name)`
accessor, the `subtask_agent_directory`, and the per-helper
`structured_output_schema`. It is control-tool-specific by design; the
generic-slots rule constrains `SessionBuildContext`, not this type.

### A mount self-gates and carries two priorities

`ControlToolMount` = `name`, `schema` (the materialized provider-facing dict),
`translate` (a closure over its own build inputs), `routing_priority`,
`schema_priority`, and an optional `answer_codec`. Returning `None` is the
universal "not applicable": a mount gates itself on its own activation name, the
kernel gates for no mount and names no control tool. Mounting **is** enablement,
so the dispatcher carries no `enabled` predicate — a disabled tool contributes
no spec.

Two priorities, because the two orders genuinely differ and both reach recorded
bytes (control schemas extend `View.provider_tool_schemas` and fold into the
stable-prefix hash):

- **routing** (translate dispatch): ask 100, todo 200, spawn 300, skill 400,
  workflow 500;
- **schema** (render order): spawn 100, todo 200, ask 300, skill 400,
  workflow 500, structured_output 600.

Both bands are locked by byte goldens, not by convention. Because a translate
closure captures its own build inputs — the skills mount closes over the
registry its own session pack merged, the delegation mount over the spawn
directory — the runtime `ControlTranslateContext` carries no feature-named
field: only `response`, `assistant_message`, `assistant_thinking` and
`content_store`.

`structured_output` mounts with `translate=None`: it is schema-only, because
react's `StructuredOutputPolicy` intercepts the call, and its gate is
data-driven (a per-helper schema is present) rather than an activation. A `None`
translate is excluded from the routing band.

### The ask answer codec is a typed field on the mount

`AskAnswerCodec` bundles the three answer-side functions — `question_handle`,
`load_questions_body`, `normalize_answer_document`. The `ask_user_question`
built-in fills it beside its own schema and translate; the builder threads it
onto `SessionInputs.answer_codec`, the host onto the Engine, and the interaction
driver reads it when an answer is submitted, **failing loudly** when it is
absent. At most one mount may contribute a codec; a second is a loud build
error.

### Packaging follows the activation name

The bodies live in the built-in named for the activation that gates them:
`todo_write`, `ask_user_question` and `delegation` (`spawn_subagent`) each own
one; `skill` belongs to the `skills` built-in under the `skill_invocation`
activation; `run_workflow` and `structured_output` belong to `react`. Each
tool's description resource ships beside its implementation. What stays in the
kernel's control band is mechanism only, plus the handful of recorded-wire names
the subtask drain and the resolver must route on and one fan-out switch two
different plugins read.

### Identity is the activation tuple

`AgentSpec.plugins` is the **full resolved** activation, sorted: the default
tool packs, the feature bundles the agent opens, the delegation activation
derived structurally from its roster, and any plugin-forced flags, all folded in
at compile time. `spawnable` is a separate structural field derived from the
roster, never an activation. Feature gating is a membership test through
`agent_activates`; there is no parallel set of booleans.

Defaults enter identity deliberately: a change to the default activation set is
then an honest identity change that turns over every bare agent's cache prefix
visibly, instead of hiding behind an invisible fold.

`AgentSpec.from_dict` requires an explicit `plugins` key and rejects a spec dict
that lacks one, loudly. An agent with no activation says so explicitly.

## Rationale

- **Producing a Decision is not a reason to stay in the kernel.** The `policy`
  surface accepts third-party code whose entire output is neutral Decisions; a
  control-tool translate is the same trust class. Keeping control tools
  kernel-permanent would preserve one hardcoded island in an otherwise generic
  builder while upholding no invariant the surface mechanism does not.
- **Two explicit priorities are the honest model of two orders.** Encoding each
  as an integer on the mount, pinned by goldens, turns a drift into a test
  failure rather than a silent change to recorded bytes.
- **Closures keep the mechanism feature-free.** Once a translate carries its own
  build inputs, the dispatcher and its context know nothing feature-named —
  which is the whole point of holding the bodies outside.
- **One identity axis, single-sourced.** The activation the loader computes *is*
  the identity, so a second representation would be a copy to keep in sync, and
  a silent default fold would let behaviour shift without the cache prefix
  moving.
- **A typed codec field fails at build time.** The driver's answer path is a
  kernel seam with a fixed shape; a typed mount field makes a missing or
  duplicated codec a build error, where an untyped bag would surface it only
  when a user finally answers a question.

## Alternatives considered

1. **An `ExecuteDecision` plus an `execution_strategy` surface** with plugin
   coroutines awaiting subtasks. Rejected: the Engine main loop is fixed, and
   suspend/resume is snapshot + wake + fold rather than a frozen coroutine (see
   `workflow-orchestration.md`). A different execution brain is expressible on
   the existing `policy` surface.
2. **An opaque `namespaces` bag on `TaskState`** for control state. Rejected: it
   breaks the fixed `TaskState` field order the stable prefix depends on and the
   optional-trailing-field folding line, and it buys nothing — the kernel stores
   todos, decisions and pending questions without interpreting them.
3. **Riding the `session_pack` surface instead of declaring a new one.**
   Rejected: control schemas need post-tool-assembly inputs (the skill menu, the
   spawn directory, the per-helper schema), and `SessionBuildContext` carries
   generic slots only by contract. A control-tool-specific context in the
   builder's post-tools phase keeps that red line intact.
4. **A frozen capability dataclass as the folded identity representation.**
   Rejected: it stores the same information twice — the activation and a bool
   set — and single-source wins.
5. **A tolerant `from_dict` that infers an empty activation when the key is
   absent.** Rejected: identity is the one field that must never be guessed; a
   silent default would make an agent's behaviour change without its recorded
   identity changing.
6. **A stringly `exports` bag for the ask answer codec.** Rejected: the codec
   has exactly one consumer and a fixed shape, so a typed field costs nothing
   and catches absence and duplication at build time.

## Consequences

- The mechanism is pure surface plumbing in the kernel: the surface entry in the
  standard catalogue, the build context / mount / entry types and the codec
  bundle in `noeta.execution.control_tool`, and the generic dual-priority mount
  loop in `noeta.execution.builder`. Mount entries arrive from two places — the
  host-resolved contributions and the entries a session pack contributes — and
  merge into one sorted loop with a collision check.
- The bodies ship in the `todo_write`, `ask_user_question`, `delegation`,
  `skills` and `react` built-ins, each with its description resource beside its
  implementation.
- `AgentSpec` identity is `plugins` plus `spawnable`, with `agent_activates` as
  the single derivation helper.
- The surface stays genuinely open: a single-file third-party plugin's control
  tool renders in the schema band, routes in the translate band, gates per
  agent, and collides loudly — with no kernel or SDK host edit.
