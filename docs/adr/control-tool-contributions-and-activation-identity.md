# Control tools are plugin contributions on the sixteenth surface; AgentSpec identity is the activation tuple

## Context

The microkernel migration (`plugin-contribution-bundles.md`, and its two
addenda) moved every official capability into a built-in plugin and made the
kernel builder generic — except for one island. Control tools
(`todo_write` / `ask_user_question` / `spawn_subagent` / `skill` /
`run_workflow`, plus `structured_output`) stayed kernel-permanent, on the
explicit reasoning recorded in that ADR's *Alternatives considered* #6:
"they are renderings of kernel Decision variants, not contributions." Their
schemas, descriptions (`policies/descriptions/*.md`), and translate bodies were
hardwired runtime material in `noeta.policies`; schema assembly was a hardcoded
if-chain (`_build_control_action_schemas`); gating flowed through a
`Capabilities` dataclass → resolver `*_enabled` flags → a `ControlToggles`
bundle. The session-pack spec deferred two threads to a later round:
"control tools stay kernel-permanent" and "`Capabilities` stays a fixed frozen
dataclass."

This ADR records the decision that closed both. It rests on two facts that the
earlier "renderings, not contributions" call had not weighed:

- A control tool's translate returns a **neutral Decision** — exactly the trust
  class of the already-open `policy` surface. Being a rendering of a Decision
  variant is not a reason it cannot be a contribution; the `policy` surface is
  the proof.
- Control schemas are **session-state functions**, not static dicts:
  `skill_tool_schema(menu)`, `spawn_subagent_tool_schema(agent_directory)`,
  `structured_output_tool_schema(schema)`. Those inputs exist only **after**
  tool assembly, which is why this is a surface running in the builder's
  post-tools phase, not a rider on `session_pack` (whose `SessionBuildContext`
  carries generic slots only, by contract).

Nothing was published and all recordings are local, so the owner chose a clean
break over a permanent compatibility table.

## Decision

### The sixteenth surface: `control_tool`

`control_tool` is the sixteenth standard surface (`SurfaceSpec` in
`noeta.client.surfaces`): plane **identity**, `activation_scope` **per-agent**,
`collision_key` **`name`**, `merge_rule` **append**, ordering **`priority`**.
A contribution is a factory `(ControlToolBuildContext) -> ControlToolMount | None`
resolved by the loader like any other surface and run by the kernel builder in
the **post-tools phase** (replacing `_build_control_action_schemas`).
`None` is the universal "not applicable" answer: a **mount self-gates** on its
context, and the kernel never gates for a mount. The surface is **open** to
third-party plugins — a translate returns neutral Decisions, the same trust
class as `policy`.

`ControlToolBuildContext` (frozen, kernel-built before the mount loop) carries
the resolved activation set, the already-ANDed effective flags (agent
activation × host kill-switches, from the resolver), the
`subtask_agent_directory`, the per-helper `structured_output` schema, and the
session packs' `exports` bag (so the skills mount reads its own skill menu).
This type is control-tool-specific by design — the generic-slots red line
protects `SessionBuildContext`, not this context.

### The mount: two priorities, self-gating, feature-free translate

`ControlToolMount` = `name`, `schema` (the materialized provider-facing dict),
`translate` (a closure over its own build inputs), `routing_priority`,
`schema_priority`. A mount carries **two** explicit priorities because the two
orders genuinely differ and both feed recorded bytes (control schemas extend
`View.provider_tool_schemas` and fold into the stable-prefix hash):

- **routing** (translate dispatch order): ask=100, todo=200, spawn=300,
  skill=400, workflow=500;
- **schema** (render order): spawn=100, todo=200, ask=300, skill=400,
  workflow=500, structured_output=600.

Both bands are **locked by the byte goldens** recorded pre-migration
(`tests/test_control_tool_schema_goldens.py`), not by convention. Because a
translate **closure captures its own build inputs** (the skill menu, the spawn
directory), the runtime `ControlTranslateContext` sheds its feature-named
`skill_menu_names` field and keeps only `response` / `assistant_message` /
`assistant_thinking` / `content_store` — the mechanism stays feature-free.

`structured_output` mounts with `translate=None`: it is **schema-only**, because
react's `StructuredOutputPolicy` intercepts it (its gate is data-driven — a
per-helper schema present — not activation). A `None` translate is excluded from
the routing band and contributes only its schema.

### Packaging by activation name

The bodies move out of `noeta.policies` and into built-ins named for their
activation:

- three new built-ins — `noeta/builtins/todo_write/`,
  `noeta/builtins/ask_user_question/`, `noeta/builtins/delegation/`
  (spawn_subagent);
- `skill` joins `noeta/builtins/skills/` (gated by the `skill_invocation`
  activation name, which stays a recognized non-plugin activation);
- `run_workflow` + `structured_output` + the workflow determinism sandbox
  (`workflow_sandbox.py`) join `noeta/builtins/react/`.

The catalogue is **seventeen** built-ins. Each tool's description `.md` ships
beside its impl (`policies/descriptions/` is deleted from the runtime wheel).
What remains in `noeta.policies` is **mechanism only**: the spec/mount types +
`ControlTranslateContext` + the `translate_control_tool` dispatcher + `stub`,
plus three reserved **recorded-wire constants** it cannot avoid touching
(`SPAWN_SUBAGENT_TOOL` / `RUN_WORKFLOW_TOOL` / `WORKFLOW_AGENT_NAME` — the drain
and resolver are mechanism that must route on recorded tool names; the
react-pin precedent) and the shared fan-out switch `concurrent_fanout_enabled`
(read by two different plugins).

### Mount-level `exports`

A `ControlToolMount` carries an `exports` mapping under a **closed vocabulary**,
the same admission rule as `PackContribution.exports`: a key is admitted only
when a kernel seam consumes it. The sole tenant is
`CONTROL_EXPORT_ASK_ANSWER_CODEC`: the ask answer codec
(`load_questions_body` / `normalize_answer_document` / `question_handle`) lives
in the `ask_user_question` built-in beside its schema and translate, and reaches
the driver via the mount export — `SessionInputs.control_exports` →
`Engine(answer_codec=…)` → the `InteractionDriver`, which **fails loudly** when
the codec is absent.

### Identity is the activation tuple

`Capabilities` and `ControlToggles` are **deleted**. `AgentSpec` carries the
activation directly:

- `plugins: tuple[str, ...]` — the **full resolved** activation, sorted,
  **including** `DEFAULT_PLUGINS`, the delegation activation derived from the
  `agents` dict, and any plugin-forced `capability_flags`, all folded in at
  compile time (`_activation_tuple`). The D6 union lives **once**, in the tuple —
  no invisible defaults and no parallel bool set.
- `spawnable: tuple[str, ...]` — a direct structural field derived from the
  `agents` dict, never an activation.

`agent_activates(agent, plugin)` is the single derivation helper (a membership
test); consumers switch to tuple reads (`"memory" in spec.plugins`). The
engine cache key's ask dimension derives from the tuple — **in-memory only, its
semantics unchanged**, no durable effect.

`DEFAULT_PLUGINS` now **enters identity** (it is part of the resolved tuple).
This is deliberate: a future change to the default set is then an **honest
identity change** — every bare agent's cache prefix turns over visibly, rather
than a default shift hiding behind an invisible fold.

### The deliberate hard break

There is **no compatibility decoder**. `AgentSpec.from_dict` **rejects the
pre-swap `capabilities` shape loudly** (and rejects a missing `plugins` key) —
a tested contract (`test_from_dict_hard_break_on_stale_capabilities`).
Recordings made before this change no longer decode. This is an owner call:
nothing is published and all recordings are local, so a clean codec beats a
permanent mapping table for recordings nobody keeps. Test fixtures and the
prompt snapshots were re-recorded **deliberately and reviewed** (the S3 diff
shows only the capabilities→plugins/spawnable representation; `system_prompt`
and tool bytes stay identical), never silently regenerated.

This break touches **no event schema**. `AgentBoundPayload` carries only
`agent_name` — the AgentSpec dict codec is the entire sanctioned break surface,
and no event-log fixture existed to break. The `TaskStatePatched` zero-drift
folding line (recorded in `control-tools-neutral-mechanism.md`) is a different
record and stays intact.

## Rationale

- **"Rendering of a Decision variant" was never a bar to being a contribution.**
  The `policy` surface already accepts third-party code whose output is neutral
  Decisions; a control-tool translate is the same trust class. Keeping control
  tools kernel-permanent duplicated the builder's last hardcoded island for no
  invariant that the surface mechanism does not already uphold.
- **The two-priority mount is the honest model of the two orders.** Routing and
  schema orders differ, and both feed recorded bytes. Encoding each as an
  explicit integer on the mount — locked by the pre-migration goldens — makes a
  drift a golden failure, not a silent byte change.
- **Feature-free mechanism.** A translate closure that captures its own build
  inputs lets `ControlTranslateContext` drop `skill_menu_names`; the runtime
  dispatcher then knows nothing feature-named, which is the whole point of
  demoting the bodies to plugins.
- **One identity axis, single-source.** Folding activation into one tuple
  removes the `Capabilities` bools that duplicated the same information; the
  activation the loader already computes **is** the identity, so there is no
  second representation to keep in sync.
- **A clean break is cheaper than a permanent table.** With nothing published,
  a compatibility decoder would be a table maintained forever for recordings
  that do not exist off local disk. The loud `from_dict` rejection turns the
  break into a reviewed re-record instead of a silent tolerance.

## Alternatives considered

1. **An `ExecuteDecision` + `execution_strategy` surface** with plugin
   coroutines awaiting subtasks. Rejected: the Engine main loop is locked, and
   suspend/resume is snapshot + `wake_on` + fold, not a frozen coroutine — the
   frozen-coroutine execution model was already rejected by
   `workflow-orchestration.md`. Anyone wanting a different execution brain uses
   the existing `policy` surface.
2. **An opaque `namespaces` bag on `TaskState`.** Rejected: it breaks the fixed
   `TaskState` field order (the stable prefix) and the optional-trailing-field
   zero-drift folding line, and it buys nothing — the kernel already stores
   control state (todos / decisions / pending questions) without interpreting
   it.
3. **Riding the `session_pack` surface instead of a new one.** Rejected: control
   schemas need **post-tool-assembly** inputs (the skill menu, the spawn
   directory, the per-helper schema), and `SessionBuildContext` carries generic
   slots only by contract. A control-tool-specific `ControlToolBuildContext` in
   the builder's post-tools phase keeps the generic-slots red line intact.
4. **Keeping `Capabilities` as the folded identity representation.** Rejected by
   the owner: it stores the same information twice (the activation tuple **and**
   a bool set), violating single-source. (This is a reversal of
   `plugin-contribution-bundles.md` D5, which had kept `Capabilities` as the
   internal folded form; see that ADR's Consequences current-state note.)
5. **A tolerant decoder for old `AgentBound` / spec dicts.** Rejected by the
   owner: a permanent mapping table for recordings nobody keeps. The hard break
   with a loud `from_dict` rejection is the deliberate choice.

## Consequences

- The mechanism lands in the kernel as pure surface plumbing: the `control_tool`
  `SurfaceSpec` (`noeta.client.surfaces`), `ControlToolBuildContext` /
  `ControlToolMount` / `ControlToolEntry` and `CONTROL_EXPORT_ASK_ANSWER_CODEC`
  (`noeta.execution.control_tool`), and the builder's generic dual-priority
  mount loop (`noeta.execution.builder`). `noeta.policies` shrinks to
  `control_semantics` (mechanism + the three reserved constants +
  `concurrent_fanout_enabled`) + `stub`; `policies/control_tools.py`,
  `policies/_control_translate.py`, and `policies/descriptions/` are deleted.
- The bodies land in the noeta-sdk wheel: `todo_write` / `ask_user_question` /
  `delegation` / `skills` / `react` built-ins, each with its description `.md`
  beside its impl. The built-in catalogue is **seventeen**.
- `AgentSpec` identity is `plugins` + `spawnable`; `Capabilities` and
  `ControlToggles` no longer exist. `AgentSpec.from_dict` is a hard break on the
  pre-swap `capabilities` shape. `DEFAULT_PLUGINS` entering identity means a
  future default change is a visible cache-prefix turnover.
- CONTEXT.md carries the vocabulary: the `control_tool` surface joins Surface /
  SurfaceSpec (fifteen → sixteen), the built-in catalogue count moves to
  seventeen, the `noeta.policies` control-band description becomes
  "mechanism + reserved constants," Activation becomes tuple identity, and a
  new "Control tool mount" term defines the surface / factory / mount /
  dual-priority / exports shape. `Capabilities` and `ControlToggles` are retired
  from the vocabulary.
- This ADR **extends** `plugin-contribution-bundles.md` (the surface-registry
  ADR) with a sixteenth surface and completes the `Capabilities`-retirement
  thread its D5 left open. It **supersedes** the "control tools stay kernel"
  reasoning in `control-tools-neutral-mechanism.md` and
  `plugin-contribution-bundles.md` *Alternatives* #6, while leaving their neutral
  `Decision`-union story (and its `TaskStatePatched` zero-drift line) untouched.
- The proof that the surface is genuinely open is one extension test
  (`tests/test_control_tool_extension.py`), mirroring
  `test_session_pack_extension.py`: a single-file plugin's control tool renders
  in the schema band, routes in the translate band, gates per-agent, and
  collides loudly — with zero kernel or SDK-host edits.

## Addendum (2026-07-30) — the flag bag fold

A follow-up review pass closed the one asymmetry this ADR shipped with: the
kernel builder still enumerated the five control-tool enablement flags by name
(`todo_write_enabled` / `ask_user_question_enabled` / `delegation_enabled` /
`skill_invocation_enabled` / `workflow_enabled`) as `build_session_inputs`
keywords and `ControlToolBuildContext` fields, while session packs read the
generic `capability_flags` bag.

- The five keywords are **deleted**; the host supplies the already-ANDed
  effective values as entries in the same `capability_flags` mapping session
  packs read (`"todo_write"` / `"ask_user_question"` / `"delegation"` /
  `"skill_invocation"` / `"workflow"`, beside `"memory"` / `"browser"`).
  `ControlToolBuildContext` carries the bag plus a `flag(name)` helper; a
  mount self-gates with `ctx.flag("<its own activation name>")`, so a
  third-party control tool now gates on its own activation with **zero kernel
  signature change** — previously impossible without borrowing a built-in's
  flag.
- The builder's `_skill_menu` special case is **deleted**: the skills mount
  derives its menu itself from its own pack's `EXPORT_SKILLS_KIT` export
  (`ControlToolBuildContext.exports`), which is what the S1 design comment
  already anticipated. The kernel builder now names no control tool and no
  control-tool flag.
- Bytes are unchanged — the S0 schema goldens, the composer View snapshot,
  and the session-pack goldens all pass unmodified. The resolver↔host
  `_build_engine` hook contract (which computes the depth-masked
  `ask_user_question` and host-gated `delegation` effective values) is
  unchanged; only the builder-facing carrier moved.
