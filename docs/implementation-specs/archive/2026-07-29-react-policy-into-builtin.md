# ReActPolicy moves into a `react` built-in (microkernel phase 2b)

> **Status: Shipped** — landed in the phase-2b commit (this session,
> after phase 2a `2373217`); the durable decisions live in the
> [plugin-contribution-bundles.md](../../adr/plugin-contribution-bundles.md)
> microkernel addendum (Scope bullet updated: `noeta.policies` is the
> control band, the catalogue holds thirteen).

## Goal

The official decision-mapping policy — `noeta.policies.react` (ReActPolicy)
plus its workflow companion `noeta.policies.orchestration` — moves into
`noeta/builtins/react/impl/` (a NEW built-in dir, catalogue 12 → 13). After
the move, `noeta.policies` is the **control band**, kernel-permanent:
`control_tools` / `control_semantics` / `_control_translate` / the control
descriptions / the protocol-level `stub` test policy. The kernel builder's
default-policy construction becomes an injection.

Deferred from phase 1 (D3: "entangled with driver/resolver and the control
tools"). The entanglement audit (2026-07-29, this session) shrank the problem:

- `MultiTurnReActPolicy` is NOT react material — it lives in
  `noeta.execution.multi_turn`, wraps *any* Policy, and only intercepts
  `FinishDecision`. The driver's import is kernel-internal. (Misleading name;
  renaming is out of scope.)
- The driver/resolver's only `noeta.policies` edges are `control_tools`
  (stays kernel per D3). The real severs are: `execution/builder.py`
  (constructs `ReActPolicy` inline as the default + imports
  `spawn_subagent_tool_schema` from react) and `execution/subtask_drain.py`
  (imports `SPAWN_SUBAGENT_TOOL` from react).

## Decisions (2026-07-29, pattern-derived from phase 1)

- **P-D1 — movers.** `policies/react.py`, `policies/orchestration.py`,
  `policies/_workflow_sandbox.py` → `noeta.builtins.react.impl.*`. New
  built-in dir `react` with a declaration-free manifest (browser/app/skills
  precedent — `POLICY_REF ("react", "1")` is already baked SDK-side in
  `parts`; the dir is the impl home + catalogue entry). `react` joins
  `_INERT_BUILTIN_ACTIVATIONS`; the catalogue containment assertion updates.
- **P-D2 — spawn-subagent vocabulary sinks kernel-side.** `SPAWN_SUBAGENT_TOOL`
  + `spawn_subagent_tool_schema` move into `noeta.policies.control_tools`:
  subagent dispatch is a rendering of the kernel's delegation mechanism
  (subtask_drain and the builder are kernel consumers) — the same judgment
  that keeps `todo_write` / `skill` / `ask_user_question` kernel-permanent.
- **P-D3 — builder seam.** `build_session_inputs` takes
  `default_policy_factory` (loud-fail `None`); the existing
  `policy_factory_override` (Options.policy / the single plugin `policy`
  contribution, D10) keeps priority over it. The factory signature is the
  builder's current inline construction, parameterized (llm, tools,
  system_prompt, model, max_steps + the react-specific knobs the builder
  passes today). SDK injects `parts.default_policy_factory()` (memoized
  dynamic resolution). `testing/profile.build_policy_factory` resolves
  through the dynamic doorway at call time (M2 guards precedent).
- **P-D4 — stays kernel.** `control_tools`, `control_semantics`,
  `_control_translate`, `policies/descriptions` (control-tool prose),
  `stub.py` (protocol-level test support, the PassthroughComposer analogue).
  react.py importing kernel `noeta.policies.*` after the move is the normal
  downward edge (builtins sit on top).
- **P-D5 — byte-identity bar.** Parity goldens (POLICY_REF in every
  AgentSpec), the composed-request snapshots (control-tool schemas render
  kernel-side, unchanged), and the ReAct prompt bytes (move code, not text).

## Sever list (grep-verified 2026-07-29, second pass)

- `execution/builder.py` — imports `ReActPolicy` + `spawn_subagent_tool_schema`
  (line ~74; schema used at ~905, construction at ~1352). NOTE: the default
  is a **closure** (`_default_react_factory(llm)`) over many kernel-computed
  kwargs (control flags, `skill_menu_names`, `content_store`, the compaction
  knobs, `output_schema` / `thinking` / `effort`). The injected seam is
  therefore a *factory builder*: `default_policy_factory(**kernel_kwargs) ->
  (llm -> Policy)` — the builder passes exactly the kwargs it passes today,
  protocols-typed, and keeps the `policy_factory_override` priority.
- `execution/subtask_drain.py` — `SPAWN_SUBAGENT_TOOL` (line 32; sink to
  `control_tools` per P-D2 alongside `spawn_subagent_tool_schema`).
- `client/host.py` — statically imports `noeta.policies.orchestration`
  (line 45: `WORKFLOW_SYSTEM_PROMPT` + the workflow policy pieces used by
  `_build_orchestration_engine`); after the move this resolves through a
  `parts` accessor (react/orchestration impl doorway).
- `testing/profile.py` — `build_policy_factory` (dynamic doorway at call
  time, M2 guards precedent).
- `tests/test_install_smoke.py` — `_RUNTIME_ALONE_SCRIPT` imports
  `noeta.policies.react`; switch the hand-injected agent to a hand-written
  protocol-level Policy (the honest kernel-alone story, acceptance 4).
- Prose only (no code edge): `control_semantics.py` docstrings,
  `resolver.py` line 77 comment.

Tests sweep: everything importing `noeta.policies.react` /
`noeta.policies.orchestration` (test_react_engine_loop + the workflow
suites are the big consumers).

## Milestones

- [x] **R1 — vocabulary + seam.** Spawn-dispatch vocabulary into
  `control_tools`; builder takes `default_policy_factory` (loud-fail);
  subtask_drain re-pointed. Gates green (no move yet — seam first, the
  phase-1 M1 discipline).
- [x] **R2 — the move.** react/orchestration/_workflow_sandbox into
  `noeta/builtins/react/impl/`; new `react` built-in dir; parts accessor;
  testing/profile doorway; tests swept. Parity goldens 5/5 byte-identical.
- [x] **R3 — docs.** CONTEXT.md catalogue count + Policy vocabulary entry;
  reference/plugins 13-name list; ADR touch-ups if any prose names
  `noeta.policies.react`; spec ticks + archive.

## Acceptance

1. Kernel bands hold no static import of a moved policy module (universal
   contract covers it); import-linter all KEPT.
2. Parity goldens 5/5 + composed-request snapshots byte-identical;
   `make check` green.
3. Bare `Options()` and every preset behave unchanged; `Options.policy` and
   the plugin `policy` surface (D10) still override the default.
4. Runtime wheel no longer ships react/orchestration; runtime-alone smoke
   updated if it imports `noeta.policies.react` (it does — the hand-injected
   agent constructs ReActPolicy; EITHER the smoke hand-writes a trivial
   Policy against the protocol, which is the honest kernel-alone story, OR
   it keeps a kernel-shipped test policy — decide in R2 and record here).

## Risks

- **The builder's inline ReActPolicy args** are computed mid-build (skill
  allowed-tools, control schemas, compaction knobs): the factory signature
  must carry them without leaking impl types INTO the kernel signature —
  protocols-only parameters.
- **subtask/workflow paths** construct policies in resolver/drain flows;
  every construction site must route through the injected factory (grep
  `ReActPolicy(` after R2).
- **The runtime-alone install smoke** currently proves "hand-injected agent
  runs" WITH ReActPolicy; after the move that exact proof must switch to a
  protocol-level policy or the closure breaks (acceptance 4 above).

## Progress log

- **2026-07-29 — R1+R2+R3 landed** (one commit, the 2a precedent). R1:
  `SPAWN_SUBAGENT_TOOL` / `spawn_subagent_tool_schema` were ALREADY defined
  kernel-side (`control_semantics`, re-exported via `_control_translate`) —
  react.py only re-exported them; the sink reduced to adding them to
  `control_tools`'s re-export and re-pointing builder / subtask_drain /
  orchestration. R2 movers: `policies/react.py` → `builtins/react/impl/
  react.py` (compat re-exports kept — it still re-exports the kernel
  control names, so mixed test imports sweep with one path substitution);
  `policies/orchestration.py` → `…/impl/orchestration.py`. **Deviation:**
  `_workflow_sandbox` did NOT move — `control_semantics` (kernel,
  permanent) imports its `check_workflow_script` for control validation,
  so the script sandbox is control vocabulary; orchestration imports it
  from the kernel. `policies/` is now the control band (control_semantics
  / control_tools / _control_translate / descriptions / stub) and its
  package docstring says so. Builder: inline `_default_react_factory`
  closure replaced by the injected `default_policy_factory`
  (`PolicyFactoryBuilder` Protocol — takes the 19 kernel-computed kwargs
  the closure captured, returns `(llm) -> Policy`; loud-fail None;
  `policy_factory_override` keeps priority). SDK: `parts.
  default_policy_factory()` + `parts.react_impl()` (host's workflow path:
  `OrchestrationPolicy` / `StructuredOutputPolicy` /
  `WORKFLOW_SYSTEM_PROMPT` resolve through the doorway); injected at both
  host build sites + the two `tests/_session_inputs.py` helpers.
  `testing/profile.build_policy_factory` resolves ReActPolicy dynamically
  at call time (M2 guards precedent). New `react` built-in dir
  (declaration-free manifest, catalogue 13, `_INERT_BUILTIN_ACTIVATIONS`
  += "react"). Acceptance-4 decision recorded: the runtime-alone install
  smoke now hand-writes a protocol-level `EchoOncePolicy` — the honest
  kernel-alone story (the kernel ships NO policy; a host injects one).
  Tests: one-substitution sweep over 28 files + the exact-catalogue
  assertion + 2 examples (`spawn_subtask.py` → control_tools;
  the internal demo → the impl path). R3: CONTEXT.md (control band,
  catalogue 13), reference/plugins en+zh (thirteen), ADR addendum scope
  bullet, both wheel READMEs.

  Gates: 3377 passed / 129 skipped (unchanged — pure moves), parity
  goldens 5/5 + snapshots byte-identical, install smoke 2/2 (runtime-alone
  runs the hand-written policy to TaskCompleted; react absent from the
  kernel wheel), coverage 87.59%, mypy strict clean, import-linter 10/10
  KEPT.
