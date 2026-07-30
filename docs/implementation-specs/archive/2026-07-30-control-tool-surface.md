# The `control_tool` surface — control tools become plugin contributions; AgentSpec identity becomes the activation tuple

> **Status: Shipped** — landed on `main` as the commit series e7261bb (spec) →
> fb10010 (S0 goldens) → 8b7cd02 (S1 mechanism) → e9cd54e (S2a built-ins) →
> f7cab03 (S2b moves/deletions) → 26ec35c (S3 identity swap) → the S4 docs
> commit. The durable decisions live in
> [control-tool-contributions-and-activation-identity.md](../../adr/control-tool-contributions-and-activation-identity.md);
> CONTEXT.md carries the vocabulary. Supersedes, by explicit owner decision, two
> deferrals in the session-pack spec's non-goals
> ([2026-07-30-session-pack-surface.md](2026-07-30-session-pack-surface.md)):
> "control tools stay kernel-permanent" and "Capabilities stays a fixed frozen
> dataclass".

## Goal

The five registry control tools (`ask_user_question`, `todo_write`,
`spawn_subagent`, `skill`, `run_workflow`) plus `structured_output` become
**plugin contributions on a new sixteenth surface, `control_tool`**; the
runtime keeps only the neutral dispatch mechanism (spec/mount types,
`ControlTranslateContext`, the priority-ordered translate loop). Gating and
agent identity are driven by the **activation tuple**: `ControlToggles` and
the `Capabilities` dataclass are deleted, and `AgentSpec` carries
`plugins: tuple[str, ...]` + `spawnable: tuple[str, ...]` directly. After
this lands, adding or changing a control tool is a plugin edit, and
`noeta.policies` holds zero product schemas, descriptions, or translate
bodies.

Today's state, for scale: `_CONTROL_TOOL_SPECS` is already an ordered
registry with per-tool `ControlToolSpec(name, enabled, translate)` entries
and a single dispatcher `translate_control_tool`
(`packages/noeta-runtime/noeta/policies/control_semantics.py:213/238/283`) —
but the specs, schemas, descriptions (`policies/descriptions/*.md`), and
translate bodies are hardwired runtime material, schema assembly is a
hardcoded if-chain (`execution/builder.py:457`
`_build_control_action_schemas`), and gating flows through
`Capabilities` → resolver `*_enabled` flags → `ControlToggles`.

## Non-goals

- **The Decision union is untouched.** No `ExecuteDecision`, no merge of
  `SpawnSubtaskDecision`/`SpawnSubtasksDecision`; the union stays "the set
  of neutral mechanism variants" (ADR control-tools-neutral-mechanism).
- **No `execution_strategy` surface.** The Engine main loop and
  Dispatcher/Worker/Lease stay locked; workflow execution stays "spawn the
  reserved `__workflow__` child running `OrchestrationPolicy`" with
  stop-and-go via re-run-from-the-top + EventLog-as-journal (ADR
  workflow-orchestration). Anyone wanting a different execution brain uses
  the existing `policy` surface.
- **`TaskState` / `TaskStatePatch` fields are untouched.** No `namespaces`
  bag; `todos`/`decisions` stay typed; the `TaskStatePatched` zero-drift
  folding line (optional trailing fields, no new event type) still holds —
  this track changes no event on that path.
- **The kernel's `__workflow__` child-engine build machinery stays kernel**
  (resolver/driver/subtask_drain). Same class of acknowledged entanglement
  as the `POLICY_REF ("react", "1")` pin: react is the undisablable default
  brain.
- **Host kill-switches stay host config.** `delegation_allowed` /
  `workflow_allowed` (`HostConfig`) remain operator authority, never a
  plugin surface; `workflow` remains host-wired, not an activation.
- **The noeta-agent sibling repo sweep is a follow-up**, not gated here (it
  already has a pending import sweep for the deleted parts accessors).

## Context

- Facts anchoring the design (verified 2026-07-30):
  - Translate routing order (position in `_CONTROL_TOOL_SPECS`):
    `ask_user_question` → `todo_write` → `spawn_subagent` → `skill` →
    `run_workflow`. Schema render order (`_build_control_action_schemas`):
    `spawn_subagent` → `todo_write` → `ask_user_question` → `skill` →
    `run_workflow` → `structured_output`. **Two different orders**; both
    feed recorded bytes (control schemas extend
    `View.provider_tool_schemas` and fold into the stable-prefix hash,
    `context/composer.py:910`).
  - Schemas are session-state functions, not static dicts:
    `skill_tool_schema(menu)` (menu from the skills registry, built by the
    skills session pack), `spawn_subagent_tool_schema(agent_directory)`
    (from `spawnable` + roster), `structured_output_tool_schema(schema)`
    (per-helper, read off `TaskCreated.inputs.output_schema`). These inputs
    exist only **after** tool assembly — which is why this is a new surface
    running in the builder's existing post-tools phase, not a
    `session_pack` rider (`SessionBuildContext` carries generic slots only,
    by contract).
  - `structured_output` is not in the translate registry (react's
    `StructuredOutputPolicy` intercepts it); its gate is data-driven
    (per-helper schema present), not activation.
  - `ReActPolicy` builds `ControlToggles` from its `*_enabled` construction
    flags (`builtins/react/impl/react.py:775`) and calls
    `translate_control_tool` per response.
  - `AgentSpec.capabilities: Capabilities` is how activation enters durable
    identity (recorded via `AgentBound`); `AgentSpec` does **not** carry the
    raw plugins tuple today. `Capabilities` =
    `todo_write/ask_user_question/delegation/skill_invocation/memory/mcp/browser`
    bools + `spawnable` (`agent/spec.py`), all a pure function of activation
    (`client/options.py` `_capabilities_from`).
  - Runtime-side consumers of the control band beyond the policy:
    `execution/builder.py` (six `*_tool_schema` imports — replaced by the
    surface loop), `execution/resolver.py` (`WORKFLOW_AGENT_NAME`),
    `execution/subtask_drain.py` (`RUN_WORKFLOW_TOOL`,
    `SPAWN_SUBAGENT_TOOL`), `execution/driver.py` (`load_questions_body`,
    `normalize_answer_document`, `question_handle` — the ask answer codec).
  - `policies/workflow_sandbox.py` has exactly two consumers, both
    react-bound after this spec: `_maybe_workflow_decision` (translate-time
    check) and `builtins/react/impl/orchestration.py` (`SAFE_BUILTINS`).
- Owner decisions taken in the shaping interview (2026-07-30): new surface
  over a `session_pack` rider; packaging by activation name; migrate
  `structured_output` too; replace `Capabilities` wholesale **this round**;
  **hard break** on old `AgentBound` decoding (no compat decoder).

## Decisions

- **D1 — the sixteenth surface.** `control_tool`: plane `identity`,
  activation scope per-agent, collision key `name`, merge `append`,
  ordering `priority`. A contribution is a factory
  `(ControlToolBuildContext) -> ControlToolMount | None` resolved by the
  loader like any other surface and run by the kernel builder in the
  post-tools phase (replacing `_build_control_action_schemas`). `None` is
  the universal "not applicable" answer — a mount self-gates on its
  context, the kernel never gates for a mount. The surface is **open** to
  third-party plugins: a translate returns neutral Decisions, the same
  trust class as the existing `policy` surface.
- **D2 — the mount carries two explicit priorities.** `ControlToolMount` =
  `name`, `schema` (the materialized provider-facing dict), `translate`
  (a closure over its build inputs), `routing_priority`,
  `schema_priority`. Built-in bands reproduce today's two orders exactly —
  routing: ask=100, todo=200, spawn=300, skill=400, workflow=500; schema:
  spawn=100, todo=200, ask=300, skill=400, workflow=500,
  structured_output=600 — locked by the S0 golden. Because translate
  closures capture their build inputs (the skill menu), the runtime
  `ControlTranslateContext` sheds its feature-named `skill_menu_names`
  field and keeps only `response` / `assistant_message` /
  `assistant_thinking` / `content_store`.
- **D3 — packaging by activation name.** Three new built-ins:
  `noeta/builtins/todo_write/`, `noeta/builtins/ask_user_question/`,
  `noeta/builtins/delegation/` (spawn_subagent). The `skill` tool joins
  `noeta/builtins/skills/` (gated by the `skill_invocation` activation
  name, which stays a recognized non-plugin activation).
  `run_workflow` + `structured_output` + `workflow_sandbox.py` join
  `noeta/builtins/react/`. Catalogue 14 → 17. Each tool's description
  `.md` moves beside its impl; `noeta/policies/descriptions/` is deleted
  from the runtime wheel.
- **D4 — `ControlToolBuildContext`.** Frozen, kernel-built before the mount
  loop: the resolved activation set, the already-ANDed effective flags
  (agent activation × host kill-switches, from the resolver),
  `subtask_agent_directory`, the per-helper structured-output schema, and
  the session packs' `exports` bag (so the skills mount reads its own
  `EXPORT_SKILLS_KIT` menu). This context is control-tool-specific by
  design — the generic-slots red line protects `SessionBuildContext`, not
  this type.
- **D5 — `ControlToggles` dies.** `ReActPolicy` receives the
  routing-ordered mounted translate specs at construction and iterates
  them; mounting **is** enablement. The runtime keeps the dispatcher loop
  and the spec/mount types as mechanism.
- **D6 — identity = the activation tuple.** `Capabilities` is deleted
  wholesale. `AgentSpec` gains `plugins: tuple[str, ...]` (sorted, the
  full resolved activation **including** `DEFAULT_PLUGINS` — no invisible
  defaults; a future default change is then an honest identity change) and
  `spawnable: tuple[str, ...]` moves up to a direct field (structural,
  derived from the `agents` dict, never an activation). Consumers switch
  to tuple reads (`"memory" in spec.plugins`); the engine cache key's ask
  dimension derives from the tuple (in-memory only, no durable effect).
  `_capabilities_from` dies; unknown activation names still fail
  compilation loudly.
- **D7 — hard break on `AgentBound`.** No compat decoder: recordings made
  before this change no longer decode. Deliberate owner call — nothing is
  published, all recordings are local. Test fixtures and the resume
  byte-equality suites are re-recorded **deliberately and reviewed**, never
  silently regenerated. The `TaskStatePatched` zero-drift line is a
  different record and stays intact.
- **D8 — kernel residue channels.** The driver's ask answer codec
  (`load_questions_body` / `normalize_answer_document` /
  `question_handle`) moves into the `ask_user_question` built-in and
  reaches the driver via **mount-level `exports`** — the same closed-
  vocabulary pattern as `PackContribution.exports`: a key is admitted only
  when a kernel seam consumes it. `RUN_WORKFLOW_TOOL` /
  `SPAWN_SUBAGENT_TOOL` / `WORKFLOW_AGENT_NAME` stay runtime-side as
  reserved vocabulary constants (react-pin precedent; the drain and
  resolver are mechanism that must route on recorded tool names).
- **D9 — hard deletes, no shims.** `policies/control_tools.py` and
  `policies/_control_translate.py` (both already re-export shims) are
  deleted; `noeta.policies` shrinks to: spec/mount types +
  `ControlTranslateContext` + the dispatcher + `stub`. Breaking change,
  `feat!` commit series like phase 3.

## Plan

- [x] **S0 — golden first.** A byte snapshot of the assembled
  `control_action_schemas` list across a full-toggle matrix (roster with
  descriptions + indexed skill menu + workflow on + per-helper
  structured-output schema), plus the all-off case. This is the lock the
  whole migration is verified against; lands before any move.
- [x] **S1 — mechanism.** Register the `control_tool` SurfaceSpec; add
  `ControlToolMount` / `ControlToolBuildContext`; builder's post-tools
  phase becomes the generic dual-priority mount loop, with the six
  existing schema functions wired as fixed internal entries (no built-in
  moves yet). S0 golden byte-identical; `ReActPolicy` switches to mounted
  translate specs; `ControlToggles` deleted.
- [x] **S2 — the moves.** Create `todo_write/`, `ask_user_question/`,
  `delegation/` built-ins; move schema + translate + description into
  each; `skill` → `skills/`, `run_workflow` + `structured_output` +
  `workflow_sandbox.py` → `react/`; ask answer codec → mount exports
  consumed by the driver; delete `policies/descriptions/`, the two shim
  modules, and every moved body. S0 golden + existing e2e suites
  byte/behavior-identical; import-linter (kernel ↛ builtins) and
  install-smoke (runtime wheel impl-free) green.
- [x] **S3 — identity.** `AgentSpec.plugins` + `spawnable`; delete
  `Capabilities`; options folding, resolver, host `capability_flags`,
  cache key switch to tuple reads; `AgentBound` fixtures and prompt
  snapshot re-pinned deliberately (hard break, reviewed diff: only the
  capabilities→plugins representation changes; `system_prompt` + tool
  bytes stay identical).
- [x] **S4 — docs.** New ADR (the `control_tool` surface + activation-tuple
  identity + the recorded hard break and its rationale); CONTEXT.md
  (sixteen surfaces, seventeen built-ins, control-band description
  rewrite, `Capabilities` / `ControlToggles` retired from vocabulary,
  Activation term updated); archive this spec as Shipped.

Each stage gates on `make check` before the next starts.

## Acceptance criteria

- [x] Bare `Options()` and the full-toggle matrix produce **byte-identical**
  control schemas pre/post S1–S2 (S0 golden), and the existing prompt /
  tool-schema snapshots pass unchanged through S2.
- [x] All five translates are behavior-identical: the existing
  ask/todo/spawn/skill/workflow e2e suites pass without edits through S2.
- [x] The runtime wheel contains no control-tool schema, description, or
  translate body; `noeta.policies` = mechanism only; import-linter and
  install-smoke stay green.
- [x] The `Options` authoring surface is unchanged:
  `plugins=("todo_write", ...)` compiles as before; unknown names fail
  loudly; `DEFAULT_PLUGINS` still yields a byte-identical bare session.
- [x] `AgentSpec` has no `Capabilities`; identity = `plugins` + `spawnable`;
  the S3 re-pin diff shows only the identity-representation change.
- [x] A third-party plugin can contribute a working control tool (schema
  rendered in band, translate routed in band) with zero kernel edits —
  proven by one extension test, mirroring `test_session_pack_extension.py`.
- [x] `make check` green at every stage boundary; ADR + CONTEXT.md landed.

## Risks

- **The AgentBound hard break silently eats old fixtures.** Mitigation: the
  S3 re-record is a reviewed commit of its own; any fixture that changes
  outside the capabilities→plugins representation fails review.
- **The two orders drift during the move.** Mitigation: S0 golden lands
  first and never regenerates until S3.
- **The mount exports channel creeps open.** Mitigation: closed vocabulary,
  same admission rule as `PackContribution.exports` — a key exists only
  when a kernel seam consumes it (the ask answer codec is the only S2
  tenant).
- **noeta-agent breaks on the deleted control band / Capabilities.**
  Accepted: the sibling sweep is already queued; this repo's gates don't
  cover it.
- **`DEFAULT_PLUGINS` entering identity** means a future default change
  changes every bare agent's identity. Accepted and documented in the ADR —
  it makes a real behavior change honest instead of invisible.

## Progress log

- 2026-07-30 — shaping interview converged (surface choice; packaging by
  activation name; `structured_output` in scope; wholesale `Capabilities`
  replacement; hard break on `AgentBound`). Spec written, status Active.
- 2026-07-30 — S0 landed: `tests/test_control_tool_schema_goldens.py` (8
  tests, hand-recorded canonical-JSON literals) pins the schema-render
  order and bytes through the `build_session_inputs` public seam +
  `composer._control_action_schemas`. Constraint discovered and adopted:
  the builder kwargs (`todo_write_enabled` … `structured_output_schema`)
  and the composer field name are part of the golden's seam — S1/S2 keep
  them stable. `make check`: 3399 passed / 129 skipped.
- 2026-07-30 — S1 landed: `control_tool` is the 16th surface;
  `execution/control_tool.py` holds `ControlToolBuildContext` /
  `ControlToolMount` (translate is Optional — `structured_output` mounts
  `translate=None`, schema-only) / `ControlToolEntry`; the builder's
  if-chain became the generic dual-priority loop
  (`_run_control_tool_mounts` + a fixed `_CONTROL_TOOL_ENTRIES` table of
  six internal factories); `ControlToggles` deleted, `ControlToolSpec`
  reduced to `(name, translate)`, `ControlTranslateContext` shed
  `skill_menu_names` (closures capture the menu); the policy seam is
  `control_translate_specs: tuple[ControlToolSpec, ...]` threaded through
  `PolicyFactoryBuilder` → `build_react_policy_factory` → `ReActPolicy`
  (not a `SessionInputs` field). S0 golden byte-identical; `make check`
  3405 passed / 129 skipped; import-linter 10/10.
- 2026-07-30 — S2a landed: `todo_write/` / `ask_user_question/` /
  `delegation/` built-ins (catalogue 14→17), descriptions git-renamed
  byte-identical; external plumbing =
  `parts.default_control_tools()` + `PluginSet.activation_control_tools()`
  + `build_session_inputs(control_tools=...)`; ask answer codec rides
  `ControlToolMount.exports` (`CONTROL_EXPORT_ASK_ANSWER_CODEC` →
  `SessionInputs.control_exports` → `Engine(answer_codec=...)`;
  `InteractionDriver.seed_answer` fails loudly when absent). Activation
  coexistence pinned: the three names still fold via
  `_ACTIVATION_CAPABILITY_FLAG` (checked first) and built-in plugins are
  excluded from `identity_activations()`. Internal entries table down to
  skill / run_workflow / structured_output. `make check` 3406 passed;
  install-smoke green.
- 2026-07-30 — S2b landed: skill → `skills/`, run_workflow +
  structured_output + `workflow_sandbox.py` → `react/` (all .md git-mv
  byte-identical); `_CONTROL_TOOL_ENTRIES` and the two shims and
  `policies/descriptions/` deleted; `noeta.policies` = `control_semantics`
  (mechanism + the three reserved constants + `concurrent_fanout_enabled`)
  + `stub` only; `PluginBuilder.control_tool` sugar + extension proof
  (`tests/test_control_tool_extension.py`, 5 tests: dual bands, per-agent
  activation gate, loud collision). Deviation: the mount-loop test needed
  a one-line import repoint (`make_skill_translate` moved) — assertions
  untouched; the S0 golden passed fully unchanged. Six docs/ files still
  reference old paths — S4 scope. `make check` 3411 passed; wheels verified
  (.md at new homes, no `policies/descriptions` anywhere).
- 2026-07-30 — S3 landed: `Capabilities` deleted; `AgentSpec` carries
  `plugins` (full resolved tuple — external plugin-forced
  `capability_flags` fold in at compile time via `_activation_tuple`, so
  the D6 union lives once, in the tuple) + `spawnable`; the one derivation
  helper is `agent_activates(agent, plugin)` beside `AgentSpec`
  (membership test). Hard break (D7) is a tested contract:
  `AgentSpec.from_dict` raises on a stale `capabilities` key AND on a
  missing `plugins` key. Discovery: `AgentBoundPayload` carries only
  `agent_name` — no event-log fixture existed to break; the AgentSpec dict
  codec is the entire sanctioned break surface. Prompt-snapshot re-pin
  reviewed: diffs show only capabilities→plugins/spawnable; system_prompt
  and tool bytes untouched. `make check` 3414 passed.
- 2026-07-30 — S4 landed (docs). New ADR
  `docs/adr/control-tool-contributions-and-activation-identity.md` (the 16th
  `control_tool` surface + activation-tuple identity + the recorded hard break,
  distilling D1–D9), added to `docs/adr/index.md`. Current-state annotations
  swept into the ten ADRs that asserted stale facts
  (control-tools-neutral-mechanism, plugin-contribution-bundles
  [fifteen→sixteen surfaces; `Capabilities` retired wholesale; "control tools
  stay kernel" reversed], workflow-orchestration, tool-and-agent-catalog,
  memory-consolidation, model-driven-skill-invocation, mcp-connectors,
  execution-environment-seam, workspace-and-session-path,
  unified-context-supply). CONTEXT.md rewritten (control band = translate
  mechanism + reserved constants only; sixteen surfaces / seventeen built-ins;
  Activation = tuple identity via `agent_activates`; new "Control tool mount"
  term). Reference/architecture docs (overview, sdk, presets, glossary,
  plugins, tools) + their `docs/zh/**` mirrors updated. All seven acceptance
  criteria verified against git log / test names / grep and ticked; this spec
  archived Shipped. `make check` 3414 passed / 129 skipped, mypy clean,
  import-linter 10/10.
