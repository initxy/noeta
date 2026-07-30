# The `session_pack` surface — the builder sheds its per-feature seams (microkernel phase 3)

> **Status: Shipped** — landed as the S1–S6 commit series
> (`861a712` → `1d0e671` → `4eaedc3` → `f22b7c4` → `0960f11`), following phase
> 2c (`c2569d7`). The durable decisions live in the
> [plugin-contribution-bundles.md](../../adr/plugin-contribution-bundles.md)
> `2026-07-30` microkernel addendum (the fifteenth standard surface; the
> builder is now generic).

## Goal

Adding or changing a session-assembled capability (a tool pack that needs
session-scoped inputs — workspace, exec env, a backend, config dirs) becomes a
plugin contribution, with **zero kernel edits**. Today it is not: the kernel
builder enumerates every capability by name — `_BuildSpec` carries thirteen
per-feature factory/kit fields, `_TOOL_PIPELINE` hardcodes nine per-feature
stages (`execution/builder.py:888`), `build_session_inputs` exposes a dozen
feature-named parameters, and the SDK host hand-wires each one through a
dedicated `parts.py` accessor. Seven of the fourteen built-in manifests
(`browser`, `app`, `mcp`, `skills`, `react`, `providers`, `workspace`) are
contribution-free shells precisely because their capability cannot be
expressed as a contribution.

The fix is not a new mechanism — it is the fourteen-surface registry we
already have (`client/surfaces.py`, ADR plugin-contribution-bundles D3:
"adding a surface = registering one SurfaceSpec"). This spec adds **one**
surface, `session_pack`: a loader-resolved factory with a uniform signature
that receives a kernel-built `SessionBuildContext` and returns a
`PackContribution`. The builder's nine stages collapse into one generic,
priority-ordered loop. The two capability-seam Protocols still parked in the
kernel (`runtime/browser.py`, `runtime/app_preview.py`) move into their
plugins, and the instructions/environment loader halves
(`execution/instructions.py` `load_*`/`discover_*`/`build_*`,
`execution/environment.py` `load_environment`) move into the `workspace`
built-in — closing the last two "change a feature ⇒ edit the kernel" gaps in
the session-assembly path.

## Non-goals

- **Capabilities stays a fixed frozen dataclass** (owner D3). Gating is
  already sufficient: plugin activation is per-agent, and a pack factory
  self-checks its context. Opening `Capabilities` into a name→bool set
  touches AgentSpec identity and fold/resume; separate spec if ever.
- **`mcp_tools_override` / `custom_tools` / `exec_env` stay kernel
  parameters** (owner D1). They are pre-built, feature-agnostic objects; the
  MCP prefix collision check is kernel vocabulary (`runtime/mcp.py` stays).
- **`policy` / `guard` / `observer` / `reminder` / `reminder_provider`
  contributions are untouched** — they already have surfaces; their builder
  parameters (`default_policy_factory`, `guards_factory`, `base_reminders`,
  `extra_guards`, `extra_reminders`) are generically typed and stay.
- **Control tools stay kernel-permanent** (ADR
  control-tools-neutral-mechanism). The control-tool registry opening is a
  separate track, not this spec. The control-band capability booleans
  (`todo_write_enabled`, `ask_user_question_enabled`,
  `skill_invocation_enabled`, `workflow_enabled`, `delegation_enabled`,
  `structured_output_schema`) remain explicit builder parameters.
- **SdkHost's public constructor API does not change.** Host config fields
  (`skills_dir`, `memory_dir`, …) keep their names; the host maps them into
  the plugin-scoped config bag internally.
- **No event-schema changes.** The typed record seams
  (`record_instructions` / `record_environment` / `record_memory_index`) and
  their event payloads are untouched; only the load halves move.

## Owner decisions (2026-07-30 interview)

- **D1 — scope: all six packs + three kits.** `fs`, `web`, `memory`,
  `skills`, `browser`, `app` tool packs and the `memory_index` /
  `instructions` / `environment` resident kits all migrate onto the new
  surface. End state: `_BuildSpec` has zero feature-named fields.
- **D2 — one deep surface.** A single `session_pack` surface whose factory
  returns a `PackContribution` struct with optional fields, rather than
  splitting tool construction and resident-kit construction into two
  surfaces. Mixed packs (memory, skills) contribute once.
- **D3 — capability flags stay fixed.** See Non-goals.
- **D4 — hard break, milestone-staged.** `build_session_inputs`'s
  signature-lock test is re-baselined deliberately per milestone (`feat!`
  commits, house phase-1/2a/2b pattern: seam first, then move, gates green
  each step). No deprecation shims — nothing is on PyPI and the only caller
  is the SDK host.

## Design decisions (derived)

- **D5 — the surface row.** `session_pack` registers in
  `standard_registry()` as: plane `wiring`, activation_scope `per-agent`,
  collision_key `name`, merge_rule `append`, ordering **`priority`** —
  integer ascending, ties broken by `(plugin, name)`, exactly the
  `reminder` / `tool_result_transform` precedent. A pack runs only when its
  plugin is activated for the agent (the existing per-agent activation
  machinery; no new gating concept).
- **D6 — the factory contract.** Kernel-defined in a new
  `noeta.execution.session_pack` module:

  ```
  SessionPackFactory = (ctx: SessionBuildContext) -> PackContribution
  ```

  `SessionBuildContext` (frozen, kernel-built inside `build_session_inputs`)
  carries only generic slots:
  - `workspace: WorkspaceRoot` — **built by the kernel before the loop**
    (today `_stage_fs_pack` builds it and later stages borrow it; that
    inversion ends — containment is kernel mechanism, packs are consumers),
  - `workspace_dir`, `content_store`, `exec_env`, `model`,
    `provider_family`,
  - `backends: Mapping[str, object]` — the named backend bag (D8),
  - `capability_flags: Mapping[str, bool]` — the derived per-agent flags,
  - `plugin_config: Mapping[str, Mapping[str, object]]` — per-plugin config
    bag (D9),
  - `allowed_tools: frozenset[str]`, plus the write/shell config block
    (`write_mode`, `shell_mode`, `shell_allowlist`,
    `shell_approval_predicate`, `write_path_globs`, `write_roots`) — shared
    safety inputs any pack may honour.

  `PackContribution` (frozen) has optional fields, each consumed by an
  existing kernel seam — the field set is closed over what the nine current
  stages actually produce, grep-verified in S1:
  - `tools: Mapping[str, Tool]` — merged in loop order; name collision
    raises,
  - `content_kinds: tuple[ContentKindSpec, ...]` — registered in loop order
    (registration order IS the semi-stable layout,
    `context/content_channel.py:93`),
  - `snapshots` — typed payloads for the existing `record_*` seams
    (instructions / environment / memory index), recorded by the kernel,
  - `memory_state` — the store/entries pair the memory intake seam consumes
    (today `MemoryFactory`'s triple, minus the tools).

  A factory that finds itself inapplicable (no backend, capability flag off,
  no dirs configured) returns the empty `PackContribution` — applicability
  is the pack's own check against `ctx`, not a kernel `if`.
- **D7 — one loop, pinned priority bands.** `_TOOL_PIPELINE` is replaced by
  a single loop over priority-sorted entries. Built-in packs pin defaults
  reproducing today's byte order: `fs=100`, `web=200`, `memory=300`,
  `instructions=400`, `environment=500`, `skills=600`, `browser=700`,
  `app=1000`. The two kernel-owned injections ride the same loop as
  internal fixed-priority entries wrapping their parameters: `mcp=800`,
  `custom=900` (custom still shadows earlier names by merging later —
  `builder.py:848` contract preserved). Tool dict insertion order feeds
  `ToolSchemaRecorded` and the stable-prefix hash
  (`context/composer.py:289`), so the order is locked by golden tests, not
  by convention.
- **D8 — the backend bag replaces the typed backend parameters.**
  `browser_backend` / `app_gateway` / `browser_enabled` die as builder
  parameters. The host populates `backends` by name: `"browser"` from the
  sandbox manager (fed by the `sandbox` built-in's existing
  `sandbox_provider` contributions `aio-exec-env` / `aio-browser`),
  `"app_preview"` from the product gateway. The `BrowserBackend` Protocol
  moves into the `browser` built-in (its five tools live there — the import
  is plugin-internal, type safety unchanged); `AppPreviewGateway` +
  `AppMount` move into the `app` built-in; the `sandbox` built-in imports
  `browser`'s Protocol for its adapter (a normal one-directional
  plugin→plugin dependency). `runtime/browser.py` and
  `runtime/app_preview.py` are deleted; the kernel holds no capability-seam
  Protocol.
- **D9 — plugin-scoped config bag.** The feature-named config parameters
  (`skills_dir`, `builtin_skills_dirs`, `global_skills_dir`,
  `skill_tool_enforcement`, `allow_skill_scripts`, `memory_dir`,
  `global_memory_dir`, `instructions_enabled`, `instructions_file`,
  `instructions_discovery`) leave the kernel signature. The host assembles
  `plugin_config = {"skills": {...}, "memory": {...}, "workspace": {...}}`
  from its unchanged public fields; each pack parses its own entry.
  `PluginManifest.config_schema` already anticipates per-plugin config; a
  validator can tighten this later.
- **D10 — loader halves move to the `workspace` built-in.** Phase 2c
  already put the instructions/environment kits in `workspace.impl`; the
  load halves follow: `load_instructions` / `discover_instructions` /
  `build_instructions_discovery` / `build_instructions_preloader`
  (`execution/instructions.py`) and `load_environment` + git/date helpers
  (`execution/environment.py`) move to `workspace.impl`. The kernel keeps
  the `record_*` seams. The host's direct loader calls (`client/host.py:2024`)
  re-point through the existing `parts.py` doorway — a legal SDK→builtins
  dynamic edge.

## Sever list (grep-verified 2026-07-30)

- `execution/builder.py` — factory Protocols `FsToolsFactory` (220),
  `WebToolsFactory` (241), `AppToolsFactory` (283), `BrowserToolsFactory`
  (297), `SkillsFactory` (348), `MemoryFactory` (371) deleted;
  `_BuildSpec` fields `fs_tools_factory`, `web_tools_factory`,
  `memory_factory`, `browser_tools_factory`, `app_tools_factory`,
  `skills_factory`, `memory_index_kit`, `instructions_kit`,
  `environment_kit`, `browser_backend`, `browser_enabled`, `app_gateway`,
  `edit_tool_mutex` (477–541, 438–457) deleted — `edit_tool_mutex` is fs
  knowledge and folds into the fs pack; stages `_stage_fs_pack`,
  `_stage_memory`, `_stage_instructions`, `_stage_environment`,
  `_stage_skills`, `_stage_browser`, `_stage_app` (592–879) collapse into
  the generic loop; `_stage_mcp` / `_stage_custom` become the two internal
  fixed-priority entries; `build_session_inputs` gains `session_packs=`,
  `backends=`, `plugin_config=` and loses every feature-named parameter
  (signature-lock test re-baselined per D4).
- `runtime/browser.py`, `runtime/app_preview.py` — deleted (D8).
- `execution/instructions.py`, `execution/environment.py` — load halves
  out per D10; `record_instructions` / `record_environment` stay;
  `execution/__init__.py` re-exports pruned.
- `client/parts.py` — `default_tool_factories` (104), `default_memory_factory`
  (313), `default_browser_tools_factory` (238), `default_app_tools_factory`
  (264), `default_skills_kit_factory` (384), `default_memory_index_kit`
  (158), `default_instructions_kit` (177), `default_environment_kit` (169),
  `edit_tool_mutex` (203) replaced by one generic session-pack resolution
  over `PluginSet`.
- `client/host.py` — per-feature arguments at the two
  `build_session_inputs` call sites (1684–1813 and the reduced resume path
  1910–1918) replaced by `session_packs` / `backends` / `plugin_config`;
  the `session_browser_backend` special case (1576–1579) becomes generic
  backend-bag population; `_skills_factory` folds into the skills pack
  (the `PluginSet.disabled_builtins` honouring moves with it).
- `client/surfaces.py` — `session_pack` SurfaceSpec added to
  `STANDARD_SURFACES` (fifteenth); `client/plugin_set.py` — per-surface
  projection (sibling of `activation_transforms`) resolving activated packs
  in `(priority, plugin, name)` order.
- Manifests — `fs`, `web`, `memory`, `skills`, `browser`, `app`,
  `workspace` gain `c("session_pack", <name>, <ref>, priority=<band>)`;
  the browser/app/skills/workspace manifests stop being contribution-free
  shells.
- Tests sweep: builder stage unit tests re-homed per pack; goldens added
  (see Acceptance); seam tests for deleted Protocols moved into the owning
  plugin's tests.

## Milestones

- [x] **S1 — seam.** `SessionBuildContext` / `PackContribution` /
  `SessionPackFactory` land in `noeta.execution.session_pack`; the
  `session_pack` SurfaceSpec (fifteenth) + `PluginSet` projection land in
  the SDK; the builder grows the generic loop **driven by internally
  wrapped legacy factories** (no manifest change yet, no behaviour change);
  stable-prefix / semi-stable / tool-order goldens recorded against
  pre-migration `main`. Gates green.
- [x] **S2 — fs/web/memory/skills migrate.** Four manifests gain
  `session_pack` contributions; host resolves packs through the generic
  projection; their legacy builder fields and `parts.py` accessors are
  deleted; `edit_tool_mutex` folds into the fs pack; skills'
  `disabled_builtins` honouring moves into the pack path. Goldens
  byte-identical.
- [x] **S3 — browser/app + backend bag.** Protocols move to their plugins,
  `runtime/browser.py` / `runtime/app_preview.py` deleted, sandbox adapter
  re-pointed at `browser`'s Protocol, host populates `backends` from
  `sandbox_provider` contributions + gateway; `browser_backend` /
  `browser_enabled` / `app_gateway` parameters deleted. Goldens
  byte-identical.
- [x] **S4 — kits + loader halves.** `memory_index` / `instructions` /
  `environment` become pack contributions (snapshots + content kinds);
  loader halves move to `workspace.impl` per D10; `client/host.py:2024`
  path re-pointed through the doorway; feature-named config parameters
  collapse into `plugin_config`. Goldens byte-identical; semi-stable
  registration order proven unchanged.
- [x] **S5 — kernel cleanup + extension proof.** Remaining feature-named
  parameters deleted; mcp/custom wrapped as fixed-priority internal
  entries; signature-lock test re-baselined; **the goal test lands**: a
  single-file `PluginBuilder` plugin contributes a toy session pack and its
  tool appears in a session with zero kernel/SDK-host edits.
- [x] **S6 — docs.** ADR plugin-contribution-bundles addendum (fifteenth
  surface, builder now generic); CONTEXT.md gains `Session pack`,
  `SessionBuildContext`, `PackContribution`, `backend bag`; this spec →
  Shipped + archived. Release rides the already-pending release chain.

## Acceptance criteria

1. `_BuildSpec` and `build_session_inputs` contain **zero feature-named
   fields/parameters** (grep gate: no `browser` / `app` / `memory` /
   `skills` / `fs_` / `web_` tokens in the builder except the control-band
   booleans and the `mcp_tools_override` / `custom_tools` parameters kept
   by D1/Non-goals).
2. `runtime/browser.py` and `runtime/app_preview.py` are gone; no kernel
   band defines or imports a capability-seam Protocol; import-linter all
   KEPT (universal `sdk-core-not-builtins` included).
3. Byte-equality goldens pass: for a default session and a
   fully-loaded session (sandbox + browser + app + skills + memory), the
   tool-schema emission order, stable-prefix hash, and semi-stable
   content-kind order are identical to pre-migration `main`.
4. Resume parity: a session recorded before the migration folds and
   resumes under the new builder with an identical tool set (the reduced
   host call path included).
5. The S5 extension-proof test passes: a third-party-shaped single-file
   plugin adds a session pack with no edits outside the plugin file.
6. `make check` green at every milestone; the signature-lock test is
   re-baselined only in commits marked `feat!`.

## Risks

- **Byte-order regression.** The whole migration rides on insertion order
  (`ToolSchemaRecorded` → stable prefix; content-kind registration →
  semi-stable). Mitigation: goldens recorded in S1 *before* any move, run
  at every milestone; priority bands pinned in one table.
- **Resume path divergence.** The reduced `build_session_inputs` call
  (resume/oneshot) must resolve the same packs from recorded activations.
  Mitigation: acceptance 4 is a dedicated parity test, not a by-product.
- **`PackContribution` field creep.** The struct could become a dumping
  ground. Guard: a field is admitted only when an existing kernel seam
  consumes it (D6's closed set); new needs go through a spec, not a field.
- **Stringly config (`plugin_config`).** Typos fail silently inside a
  pack. Mitigation: packs fail loudly on unknown keys;
  `PluginManifest.config_schema` validation is the follow-up tightening.
- **Hidden `parts.py` consumers.** noeta-agent (separate repo) may import
  deleted accessors. Mitigation: sweep noeta-agent before S2 lands; it is
  already queued for the post-release import sweep.

## Progress log

- **2026-07-30 — shaped.** Owner interview settled D1–D4 (full scope, one
  deep surface, capabilities closed, hard break); D5–D10 derived from the
  surface-registry precedent and the grep-verified wiring inventory
  (builder stages/fields, host call sites, thin manifests, ordering
  contracts). Spec written; implementation not started.

- **2026-07-30 — S1–S6 landed.** Five commits, seam-first per D4.
  **S1** (`861a712`): `SessionBuildContext` / `PackContribution` /
  `SessionPackFactory` / `SessionPackEntry` land in
  `noeta.execution.session_pack`; the `session_pack` SurfaceSpec (fifteenth)
  + the `PluginSet` projection land in the SDK; the builder grows the generic
  `(priority, name)` loop driven by internally-wrapped legacy factories (no
  behaviour change); the byte-order goldens are recorded against pre-migration
  `main`. **S2** (`1d0e671`): `fs` / `web` / `memory` / `skills` manifests gain
  their `session_pack` contributions, the host resolves packs through
  `parts.default_session_packs()`, the legacy builder fields + `parts.py`
  accessors are deleted, `edit_tool_mutex` folds into the fs pack, and skills'
  `disabled_builtins` honouring moves into the pack path. **S3** (`4eaedc3`):
  `BrowserBackend` → the `browser` built-in, `AppPreviewGateway` / `AppMount`
  → the `app` built-in, `runtime/browser.py` / `runtime/app_preview.py`
  deleted, the sandbox adapter re-pointed at `browser`'s Protocol, and the host
  populates `SessionBuildContext.backends` from the `sandbox_provider`
  contributions + the product gateway; `browser_backend` / `browser_enabled` /
  `app_gateway` builder parameters gone. **S4** (`f22b7c4`): `memory_index` /
  `instructions` / `environment` become pack contributions (content kinds +
  exported snapshots), the loader halves move into `workspace.impl` (D10), the
  `client/host.py` record path re-points through `parts.workspace_impl`, and
  the feature-named config parameters collapse into `plugin_config`. **S5**
  (`0960f11`): `PluginBuilder.session_pack()` lands, the `Client` folds
  activated external plugins' packs into per-agent `activated_session_packs`,
  the host merges them after the built-in set, and
  `tests/test_session_pack_extension.py` proves a single-file third-party
  plugin's pack tool reaches a built session with zero kernel/SDK-host edits.
  **S6**: this doc — the ADR `2026-07-30` addendum (fifteenth surface, generic
  builder), the four CONTEXT.md terms (`Session pack`, `SessionBuildContext`,
  `PackContribution`, `Backend bag`), the doc sweep, and this archive.

  **Deviation:** `PackContribution`'s side-state rides a single named `exports`
  bag (single-writer keys, the `EXPORT_*` constants — e.g. `EXPORT_MEMORY_STORE`
  / `EXPORT_INSTRUCTIONS_SNAPSHOTS` / `EXPORT_CONTENT_DISCOVERY`) rather than
  the spec's typed `snapshots` / `memory_state` fields. Same closed-set rule
  (a key is admitted only when an existing kernel seam consumes it), one uniform
  mechanism instead of two per-need fields — content kinds ride their own
  `content_kinds` tuple, everything else is one `name → object` map the
  `SessionInputs` fields read by constant.

  **Deviation:** tool merge is **later-wins**, not a loop-time raise. D6 wrote
  "name collision raises"; the landed contract preserves the pre-migration
  custom-shadows semantics (a later pack — `custom` at band 900 — may
  deliberately shadow an earlier name, exactly as `custom_tools` always did),
  and the *accidental* cross-plugin collision is caught **upstream at the
  manifest merge** (`session_pack` collides on `name`), where it names both
  sides — so the loud collision check still exists, just at the honest layer.

  Gates: 3391 passed / 129 skipped, coverage 88%, mypy strict clean, naming
  lint clean, import-linter 10 kept / 0 broken. Byte-equality goldens
  (default + fully-loaded session), resume parity, and the extension proof all
  green; commits `861a712` / `1d0e671` / `4eaedc3` / `f22b7c4` / `0960f11`.
