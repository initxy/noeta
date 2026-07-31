# Plugins are manifest-declared contribution packages over a surface registry

## Status

Active. This rewrite supersedes the *mechanism* of the original decision (the
0.4.0 typed contribution bundle: a `noeta_plugin(api)` factory with one
hardcoded `PluginAPI` method per surface). That mechanism shipped as the 0.4.0
**local** release only — no PyPI, no tag — so **no backward compatibility is
owed** and the mechanism is replaced outright, not evolved. The design that
drove this rewrite is
`docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md`; the
durable decisions D1–D6 are recorded here.

The two invariants that made the original decision (declarative, not imperative;
identity-bearing, not session-mutating) are **unchanged** — the imperative
in-loop event bus was re-litigated on 2026-07-28 and declined again (see
*Alternatives*). What changed is the *shape* of the declarative mechanism, so
that new surfaces no longer require an SDK code change, discovery no longer
requires executing plugin code, and per-agent feature gating stops being a
second, parallel mechanism.

## Context

Noeta's extension surfaces were already open as typed seams (Tool / LLMProvider
/ Policy / Guard / Observer / `ContentKindSpec` on `Options`; storage and other
host injections on `HostConfig`). The 0.4.0 plugin mechanism added the missing
*unit of packaging and discovery* — the role "extensions" play in pi, "plugins"
in Cline — and its semantics were sound: declarative, identity-bearing,
collision-loud. Its shape had three limits:

1. **One hardcoded method per surface.** `add_tool` / `add_guard` / … meant
   every new surface was an SDK code change, and a host could not define a
   surface of its own.
2. **Discovery required execution.** Knowing what a plugin contributed meant
   running its `noeta_plugin(api)` factory; the `enabled` gate rested on an
   `ast` trick over the factory body.
3. **`Capabilities` duplicated the concept.** Per-agent feature gating
   (`memory` / `browser` / `skill_invocation`) was "activate a built-in feature
   bundle" in disguise — a second mechanism for the same idea as loading a
   plugin.

Copying pi's *imperative* `ExtensionAPI` (`on("tool_call")` / `on("context")` /
`setActiveTools()`) still collides with the same three Noeta invariants it
always did — event-sourced replay, the stable-prefix KV cache, and
multi-tenant operator authority — so the declarative bet stands. The redesign
keeps the bet and rebuilds the declarative form around a **static manifest** and
a **surface registry**.

## Decision

A **Plugin** is a manifest-declared contribution package. The mechanism has five
load-bearing parts (D1–D5) plus one deliberate asymmetry in how contributions
take effect (D6).

### D1 — Plugin unit and manifest

A plugin is a package (or a single `.py` file for local/dev use) carrying a
**static manifest**:

- **Distributed form** — `[tool.noeta]` in `pyproject.toml`, mirrored into the
  wheel as package data `noeta-plugin.toml`, located via the distribution
  metadata. `read_distribution_manifest` reads it **without importing any plugin
  code**: the `noeta-plugin.toml` listed in the distribution's `RECORD` is read
  straight off disk, and an editable install (where `RECORD` may omit package
  data) falls back to `importlib.util.find_spec`, which locates a top-level
  package's directory *without executing its `__init__`*.
- **Manifest fields** — `name`, `requires-noeta` (a version range), an optional
  `config-schema`, and a tuple of contributions. Each `ManifestContribution` is
  `surface` + a `ref` (a `module:qualname` import string for code) **or** `path`
  (for a resource) + surface-specific `params` (e.g. `priority`, `seams`,
  `alias`). The manifest is **inert data**: the `ref` is a string, and nothing
  in the manifest layer imports it.
- **Single-file form** (local dirs only) — a module-level `PluginBuilder` with
  decorator sugar (`@plugin.tool`, `@plugin.reminder(priority=…)`,
  `@plugin.reminder_provider(seams=…)`, …) *is* the manifest; `builder.manifest()`
  yields the equivalent static `PluginManifest`. Reading it executes the file,
  which is acceptable because local files pass an explicit trust gate anyway
  (D4). Two module-level literal forms — `noeta_plugin_name = "…"` and
  `plugin = PluginBuilder("…")` — are extracted statically with `ast`
  (`declared_plugin_name`) so the `enabled` allow-list can gate a single-file
  plugin *before* its body runs. A publish-time packaging check (`plugin_check`,
  an M5 deliverable) is designed to derive and verify the TOML from the
  decorators; by design it is a `python -m noeta.sdk.…` entry, **not** a console
  script (there is no operator CLI).

### D2 — The surface registry (the generality mechanism)

The loader is **surface-agnostic**: it consults one `SurfaceRegistry`
(name → `SurfaceSpec`) and nothing else. A `SurfaceSpec` fully describes one
surface:

| Field | Meaning |
|---|---|
| `plane` | `identity` \| `wiring` \| `host` — mirrors the `Options` / `HostConfig` split |
| `activation_scope` | `per-agent` \| `process` \| `host-wired` — how the effect is scoped (D6) |
| `validator` | called on a **resolved** value; raises when it is not a legal member of the surface |
| `collision_key` | `name` \| `kind` \| `alias` \| `single-valued` \| `none` — the namespace two contributions clash in |
| `merge_rule` | `append` \| `single` \| `dict-merge` — *(**superseded 2026-07-31:** deleted, never read; see the SDK-layer-cleanup Addendum)* |
| `ordering` | `sorted` = `(plugin, name)`; `priority` = an integer `priority` param, ties broken by `(plugin, name)` |
| `activation_binding` | *(added 2026-07-31)* identity-plane only — which `PluginActivation` channel the contribution feeds |

`standard_registry()` seeds the standard catalogue (D3) — a fresh registry each
call. A host that owns app-plane surfaces takes `registry.copy()` and
`register`s them **before** load; the same validation / collision / ordering
pipeline runs over host-plane contributions unchanged and hands them to the
host — they never enter `Options`. Adding a future surface is registering one
`SurfaceSpec`; the loader does not change. `register` rejects a duplicate
surface name loudly (no override).

### D3 — The standard surface catalogue

Fifteen surfaces, seeded by `standard_registry()` *(**current state:** sixteen — `control_tool` joined as the sixteenth; see `control-tool-contributions-and-activation-identity.md`)*:

| Surface | Plane | Scope (D6) | Cardinality | Notes |
|---|---|---|---|---|
| `tool` | identity | per-agent | multi, name-keyed | includes tool packs |
| `agent` | identity | per-agent | multi, name-keyed | `AgentDefinition` |
| `content_kind` | identity | per-agent | multi, kind-keyed | reminder **track C** (the existing content channel) |
| `prompt_fragment` | identity | per-agent | multi, name-keyed | appended after the preset prompt, sorted `(plugin, name)` |
| `policy` | identity | per-agent | **single** | collision with the base or another active plugin = error |
| `guard` | wiring | **process** | multi | governance — see D6 |
| `observer` | wiring | **process** | multi | governance — see D6 |
| `provider` | wiring | host-wired | **single** | `Options.provider` collision = error; also the wire-level escape hatch |
| `reminder_provider` | wiring | per-agent | multi, name-keyed | recorded injection — reminder **track A** |
| `reminder` | wiring | per-agent | multi, name-keyed, `priority` | compose-time pure — reminder **track B** |
| `tool_result_transform` | wiring | per-agent | multi, name-keyed, `priority` | a ToolRuntime stage, not a hook role |
| `mcp_server` | host | host-wired | multi, alias-keyed | |
| `skills` | host | host-wired | multi, path | resource-only plugins |
| `sandbox_provider` | host | host-wired | multi, name-keyed; host selects one | |
| `session_pack` | wiring | per-agent | multi, name-keyed, `priority` | session-construction factory `(SessionBuildContext) -> PackContribution` (microkernel phase 3 — see the 2026-07-30 Addendum) |

The three reminder tracks (A = recorded `reminder_provider`; B = compose-time
`reminder`; C = the pre-existing resident `content_kind` / `ContentKindSpec`)
are the three distinct ways context enters the View; they are named in
CONTEXT.md and detailed in the redesign spec (D7–D9). This ADR records only that
each is one row of the registry.

### D4 — Sources and the load pipeline

Five sources; discovery order **never** affects the result (only error
attribution):

| # | Source | Gate |
|---|---|---|
| 0 | built-in plugins (`noeta.builtins`, D11 of the spec) | on by default; a host may disable individually |
| 1 | entry points (`noeta.plugins` group) | `enabled` allow-list |
| 2 | explicit modules / file paths | caller-specified = authorized |
| 3 | `~/.noeta/plugins/` | the user's own machine = trusted |
| 4 | workspace `.noeta/plugins/` | trust store (untrusted dir → loud warning + skip) |

The pipeline for every candidate: read the manifest (zero code execution for the
package / `.toml` forms) → `enabled` gate **before any import** → trust gate
(source 4) → resolve `ref`s → validate per `SurfaceSpec` → collision check →
deterministic merge sorted by `(plugin name, contribution name)`. Collisions —
including cross-source duplicate plugin names, and (for single-valued surfaces)
two active contributions to the same surface — are **errors naming both sides;
there is no override**. Any load fault (bad manifest, missing manifest, a broken
file) raises `PluginError` naming the plugin and **fails the client build, never
a mid-session turn** — the only silent case is the untrusted-workspace skip,
which is warned.

### D5 — Two-level model: load, then activate

The old `Capabilities` and plugin loading are unified into one load/activate
axis:

- **Load (host level)** — `load_plugins(...) -> PluginSet` decides which plugin
  code is *in the process*. A `PluginSet` is **listable and collision-checkable
  without executing plugin code**: `PluginSet.contributions()` and
  `.merged()` read only the static manifests; `.resolve()` is the single
  boundary that imports a `ref`, called at the client build, never on a turn.
- **Activate (agent level)** — `Options.plugins: tuple[str, …]` and
  `AgentDefinition.plugins` decide which loaded plugins *this agent* uses.
  Activation enters `AgentSpec` identity (as `Capabilities` flags did). Every
  activation name must be a recognised built-in feature-bundle name **or** the
  name of a plugin in the loaded set handed to `Client(options,
  plugins=<PluginSet>)`; an unknown name fails compilation loudly.
- **The `Capabilities` *recipe surface* is superseded by activation.**
  `Capabilities(memory=True)` becomes `plugins=["memory"]`; `browser` and
  `skill_invocation` likewise; the official presets now express feature gating
  through `plugins=(…)`. `Capabilities` itself is **not deleted** — it survives
  as the internal, folded identity representation on `AgentSpec` (a built-in
  activation name flips the matching identity flag and nothing else), while the
  `capabilities=` authoring field is removed — `plugins=` activation is the
  only authoring path. A
  `DEFAULT_PLUGINS = ("fs", "web")` constant makes a bare `Options()` compile
  **byte-identically** to the pre-redesign spec: `fs` / `web` are identity-inert
  (the default 11-tool set still comes from `builtin_tool_classes()`), memory /
  browser stay off, and a parity golden pins the exact `AgentSpec`.

  **Current state:** the `Capabilities` dataclass this D5 kept as the folded
  identity representation was **later deleted wholesale**
  (`control-tool-contributions-and-activation-identity.md` D6): `AgentSpec`
  identity is now the `plugins` activation tuple + `spawnable`, which folds
  `DEFAULT_PLUGINS` + derived delegation + plugin-forced flags into one tuple —
  no second bool set. Everything else in D5 (load vs activate, `DEFAULT_PLUGINS`,
  the parity golden) still holds.

### D6 — Effect scoping (the one deliberate asymmetry)

How a contribution takes effect depends on its surface's `activation_scope`, not
on a uniform rule:

| Surfaces | Rule |
|---|---|
| `tool` `agent` `content_kind` `prompt_fragment` `policy` `reminder_provider` `reminder` `tool_result_transform` | **follow per-agent activation** — feature semantics; a sibling agent that did not activate the plugin carries none of it |
| `guard` `observer` | **loaded ⇒ in force for every agent in the process.** Governance is operator authority; an agent author must not be able to opt out of compliance interception or audit by omitting an activation. `PluginSet.process_hooks()` resolves these and the `Client` folds them into the process-wide guard stack + observer subscriptions regardless of any agent's activation list |
| `provider` `sandbox_provider` `mcp_server` `skills` | host wiring; the host selects one (`Options.provider` / `HostConfig`), existing per-agent override semantics unchanged |

This asymmetry is the whole reason `guard`/`observer` are on the `wiring` plane
with `process` scope rather than `identity`/`per-agent`: it encodes that
governance is not an agent-author choice. The effect-domain rule is recorded as
an addendum on `guard-observer-hooks.md`.

### Built-in plugins ride the same path

noeta is its own first plugin author. `noeta.builtins/` is a top-of-stack band
beside `noeta.presets`; each built-in is one directory holding its manifest
**and — since the 2026-07-29 microkernel migration (see the Addendum) — its
implementation**: `noeta/builtins/<name>/__init__.py` is the zero-execution
`MANIFEST`, `noeta/builtins/<name>/impl/` is the code, and the manifest `ref`s
point at the sibling impl modules. The catalogue is inert data — listing a
built-in's contributions runs zero runtime code, and importing `noeta.builtins`
(the manifest layer) imports zero impl modules. The loader reaches
`noeta.builtins` by a **dynamic import** from `noeta.client.plugin_set`; there
is no static edge, and `.importlinter`'s `sdk-core-not-builtins` forbidden
contract enforces it — universally: *every* band, kernel included, is a source.
The catalogue currently holds fourteen built-ins — `fs`, `web`, `memory`,
`browser`, `app`, `mcp`, `skills`, `react`, `reminders`, `governance`,
`providers`, `sandbox`, `presets`, `workspace` *(**current state:** eighteen — control-tool-surface S2 added the control-tool built-ins `todo_write` / `ask_user_question` / `delegation`, and the storage-backend relocation added the declaration-only `storage`; see the 2026-07-30 storage Addendum)* — so every standard surface has a built-in declaration
ridden through the identical loader / validation / merge path as any external
plugin. Adding a first-party capability is adding a directory to the catalogue
(plus a `SurfaceSpec` registration only when a genuinely new surface is
needed).

## Rationale

- **The mechanism becomes generic without adding engine power.** A surface-
  agnostic loader over a registry means a new surface is one `SurfaceSpec`, and
  a host can define its own app-plane surfaces — yet every contribution still
  compiles into `AgentSpec` identity or is handed to host wiring, so the engine
  gains zero new consistency obligations. The constraint "no seam without a real
  substitution need" is preserved: plugins substitute nothing; they populate.
- **Discovery is execution-free by construction.** Because the manifest is
  static data read without importing the `ref`, the `enabled` gate applies
  before any plugin code runs, and `PluginSet` is fully auditable without side
  effects — closing the two holes the 0.4.0 factory had (the `ast` gate trick
  and "run the factory to see what it does").
- **One axis instead of two.** Folding `Capabilities` into activation removes a
  parallel mechanism: "which plugins does this agent use" now answers both
  "which built-in features are on" and "which third-party packages contribute,"
  with a single failure mode (unknown name → loud) and a single identity path.
- **Determinism survives.** Contributions compile into `AgentSpec` identity and
  merge order is `(plugin, name)` over static manifests, so two hosts with the
  same loaded set and activation produce byte-identical agents under any
  discovery order — replay, resume, and KV-cache reuse behave exactly as with
  hand-written `Options`.
- **The ergonomics that made pi's ecosystem work are kept** — a single file is a
  plugin, decorators are the manifest, first-party examples are the
  documentation — while the contract underneath stays declarative, so authors
  cannot break invariants they never learned about.

## Alternatives considered

1. **Imperative in-loop event bus (pi's `ExtensionAPI`), re-litigated
   2026-07-28.** `on("tool_call")` / `on("tool_result")` / `on("context")` /
   `setActiveTools()`. **Declined again.** The trigger for reopening it was the
   direct pi comparison during the design interview; the comparison is precisely
   what settled it, because pi's `ExtensionAPI` lives in pi's *app* package, not
   its engine package — pi pays the cache-invalidation cost of per-call context
   rewriting willingly on a sub-1k-token prompt, and delegates replay-safety to
   extension authors by convention. Noeta made the opposite bets (locked
   stable-prefix composer, event-sourced replay, multi-tenant operator
   authority), so an imperative bus would re-open the Mutator role that
   `guard-observer-hooks.md` cut, turn replay-safety into a documentation
   promise, and hand session-time mutation power to third-party code in a
   multi-tenant process. The power the imperative form offered has explicit,
   bounded escape routes instead: wire-level rewriting → a wrapping `provider`
   contribution; custom history compaction → a future *recorded* compaction
   seam; dynamic tool sets → a future fold-modeled activation design (like
   `active_skills`). None of these is an in-loop mutation hook.
2. **A context-filter hook (`on("context")`).** Rejected: it conflicts directly
   with the locked composer and the stable-prefix KV-cache constraint.
   Structured injection is the sanctioned path, and the redesign widens it into
   three named tracks — recorded (`reminder_provider`), compose-time
   (`reminder`), and resident (`content_kind`) — none of which perturbs the
   stable prefix.
3. **Keep the 0.4.0 hardcoded-method `PluginAPI`, add the new surfaces to it.**
   Rejected: it would have grown one more `add_*` method per surface and left
   discovery-by-execution and the `Capabilities` duplication in place. The
   redesign removes the shape, not just the gaps — a host cannot define a
   surface on a fixed-method API.
4. **Config-only plugins (declarative manifests, no code).** Rejected as the
   *whole* mechanism: a manifest cannot carry a Guard, a provider, or a
   transform. The manifest here is static *for discovery*, but a contribution's
   `ref` resolves to real code at the build boundary. The config-only niche
   (no code at all) is already served by MCP connectors and skills.
5. **Storage as a plugin surface.** Rejected on principle: the
   EventLog / ContentStore / Dispatcher triple is the truth substrate every
   plugin guarantee stands on (trust bootstrapping). It is a single host
   injection with no merge semantics — the plugin mechanism adds nothing.
   Third-party backends are ordinary packages implementing the `noeta.protocols`
   storage Protocols plus the shared domain rules in `noeta.storage.spi`, wired
   via `HostConfig`.
6. **Control tools as contributions.** Rejected: `todo_write` / `skill` /
   `ask_user_question` / the subagent dispatch tool are renderings of kernel
   Decision variants; activation gates their *visibility*, plugins never
   contribute them.
7. **An app-plane plugin API in this repo.** Rejected: each host owns its
   app-plane surfaces (routers, channels, schedules, commands) in its own
   repository. A host may *register* those surfaces into this mechanism's
   registry (D2), but their contracts are host property and never enter
   `AgentSpec` identity — the same reasoning that separated `HostConfig` from
   `Options`.
8. **A plugin registry / marketplace service, or a separate plugin repo, from
   day one.** Rejected: pip / git is the registry; first-party examples live
   in-repo because a worked example corpus is the most effective plugin
   documentation (pi's `examples/extensions/` demonstrated this).
9. **User-uploaded plugin code in a multi-tenant server.** Rejected: arbitrary
   code across tenants. Users extend through MCP connectors and skills;
   operators extend through plugins.

## Consequences

- The mechanism lands in noeta-sdk: the surface registry
  (`noeta.client.surfaces`), the static manifest reader
  (`noeta.client.plugin_manifest`), the five-source loader and `PluginSet`
  (`noeta.client.plugin_set`), the activation vocabulary and compile wiring
  (`noeta.client.options`), and the built-in catalogue (`noeta.builtins`). The
  engine and composer are untouched; the new surfaces reach the runtime as
  construction fields (`tool_result_transform` stages on `ToolRuntime`, the
  reminder registries on the composer / the intake seam).
- `.importlinter` gains the `sdk-core-not-builtins` contract next to
  `sdk-core-not-presets`: `noeta.builtins` is top-of-stack and reached only by
  dynamic import.
- Changing the loaded-and-activated plugin set changes `AgentSpec` identity —
  intended, and documented so operators expect the cache-prefix turnover.
- CONTEXT.md carries the vocabulary: **Plugin** (redefined), **PluginSet**,
  **activation**, **Surface** / **SurfaceSpec**, **Built-in plugin**, and the
  three **Reminder tracks A/B/C**; the `Capabilities` *recipe* term is retired
  in favour of activation. "plugin" remains an avoided word for MCP connectors
  (a connector is configuration, not code). **Current state:** `Capabilities`
  was later retired **wholesale** (not just the recipe term) — `AgentSpec`
  identity is now the `plugins` activation tuple + `spawnable`, and `control_tool`
  joined as the sixteenth surface; see
  `control-tool-contributions-and-activation-identity.md`.
- Whenever a new extension surface is added, deciding its `plane`,
  `activation_scope`, and whether plugins may contribute to it is part of that
  surface's design — one `SurfaceSpec` row, one D6 scope choice.

## Addendum — 2026-07-29: the microkernel migration finishes the thought

The original decision left the built-ins as *thin declarations over runtime
implementations*: the manifest lived in `noeta.builtins`, the code stayed in
kernel bands (`noeta.tools.*`, `noeta.guards`, `noeta.providers`, …). The
microkernel capability migration
(`docs/implementation-specs/archive/2026-07-29-microkernel-capability-migration.md`)
completed the inversion — *the engine hosts execution; everything an agent is
made of is a plugin, including ours*:

- **Manifest and implementation are co-located.** Every official capability
  implementation moved into its plugin directory
  (`noeta/builtins/<name>/impl/`), shipped in the **noeta-sdk** wheel. The
  noeta-runtime wheel is a pure kernel — impl-free (verified by the install
  smoke) and transport-free (`httpx` moved to noeta-sdk).
- **The defaults ride the loader.** The bare-`Options()` defaults (the 11
  fs/web tools, the default guards, the three compose-time reminders, the
  provider facts) are obtained by resolving the built-in manifests through the
  `noeta.client.parts` accessors at client build; static default tables are
  gone. `DEFAULT_PLUGINS = ("fs", "web")` keeps a bare `Options()`
  byte-identical (pinned by the parity goldens).
- **The kernel builder is injection-only.** `build_session_inputs` takes
  factories (`fs/web/browser/app_tools_factory`, `guards_factory`,
  `memory_factory`, `base_reminders`, pre-resolved provider facts) that fail
  loudly when absent; installed alone, the runtime runs an agent only with
  hand-injected protocol objects.
- **Config vocabulary sank kernel-side** so specs stay typed without the
  impls: `noeta.runtime.{workspace,shell_policy,exec_env,governance,browser,
  app_preview,mcp}` — pinned lean by the `kernel-vocabulary-diet` import
  contract (they import only `noeta.protocols`).
- **`sdk-core-not-builtins` became universal**: nothing statically imports
  `noeta.builtins` — every band is a source; the loader's dynamic `ref`
  resolution is the only doorway. This single contract now also carries the
  provider-neutral kernel↛adapter rule.
- **Scope**: phase 2 followed the same day under its own specs — the skills
  subsystem moved into the `skills` built-in (phase 2a: the kernel keeps the
  `SkillsKit` / `activate_skills` seams in `noeta.execution.skills`) and
  `ReActPolicy` + the workflow interpreter moved into the `react` built-in
  (phase 2b: the builder takes a `default_policy_factory` factory-builder
  injection; `noeta.policies` is now the control band). Control tools
  (`todo_write` / `skill` / `ask_user_question`, the spawn/workflow dispatch
  vocabulary, the workflow-script validation sandbox) stay kernel permanently
  (they are renderings of kernel Decision variants, not contributions).
  *(**Current state:** this was reversed — control tools became `control_tool`
  **plugin contributions** on the sixteenth surface, moving into their built-ins
  (`todo_write` / `ask_user_question` / `delegation` / `skills` / `react`);
  `noeta.policies` keeps only the neutral translate mechanism. Being a rendering
  of a Decision variant is not a bar to being a contribution — a translate
  returns a neutral Decision, the same trust class as `policy`. See
  `control-tool-contributions-and-activation-identity.md`.)*

## Addendum — 2026-07-30: the builder goes generic (microkernel phase 3)

The 2026-07-29 migration co-located every capability's manifest and impl but
left the kernel builder still enumerating capabilities by name — `_BuildSpec`
carried thirteen per-feature factory/kit fields and `_TOOL_PIPELINE` hardcoded
nine per-feature stages, so a new session-assembled capability was still a
kernel edit. The `session_pack` surface
(`docs/implementation-specs/archive/2026-07-30-session-pack-surface.md`) closes
that last gap — *adding or changing a session-assembled capability is now a
plugin contribution with zero kernel edits*:

- **The catalogue is fifteen surfaces.** *(**Current state:** sixteen since control-tool-surface S1 added `control_tool`.)* `session_pack` is the fifteenth
  (plane `wiring`, `activation_scope` per-agent, `collision_key` `name`,
  ordering **`priority`** — the `reminder` / `tool_result_transform`
  precedent; as written it also carried `merge_rule` `append`, deleted
  2026-07-31 — see the SDK-layer-cleanup Addendum). It is the **session-construction half**
  of a capability: a factory `(SessionBuildContext) -> PackContribution` the
  kernel builder runs when the contributing plugin is activated for the agent.
  `PackContribution` carries `tools` + `content_kinds` (each with its **own**
  registration priority, because the semi-stable layout order genuinely differs
  from the tool order) + named `exports` — every field consumed by an existing
  kernel seam. (The D3 table and its count are updated inline above.)
- **The kernel builder is generic — one priority-ordered loop.**
  `_TOOL_PIPELINE` and the nine `_stage_*` methods are gone; `build_session_inputs`
  builds the containment `WorkspaceRoot` and one frozen `SessionBuildContext`,
  then runs every `SessionPackEntry` through a single `(priority, name)`-sorted
  loop, merging tools (later-wins), content kinds (own priority), and exports.
  Built-in bands reproduce the pre-migration byte order — `fs`=100, `web`=200,
  `memory`=300, `instructions`=400, `environment`=500, `skills`=600,
  `browser`=700, `app`=1000 — with `mcp`=800 / `custom`=900 as the two
  **kernel-owned fixed-priority entries** wrapping the parameters D1 keeps
  (`mcp_tools_override` / `custom_tools`). Tool dict insertion order feeds the
  deterministic `ToolSchemaRecorded` emission and the stable-prefix hash, so the
  bands are **locked by the `test_session_pack_goldens.py` byte-equality
  goldens**, not by convention.
- **Capability-seam Protocols live in their plugins.** `runtime/browser.py` and
  `runtime/app_preview.py` are deleted; the kernel holds **no** capability-seam
  Protocol. `BrowserBackend` moved into the `browser` built-in (the `sandbox`
  built-in's adapter implements it, importing the Protocol — a normal, one-way
  plugin→plugin dependency); `AppPreviewGateway` / `AppMount` into the `app`
  built-in. The live backings ride the generic `backends` bag: the host
  populates `SessionBuildContext.backends` by the **contributing plugins' own
  names** — `"browser"` from the sandbox provider, `"app_preview"` from the
  product gateway, never the kernel's vocabulary; an absent name means the
  capability has no live backing, and the pack returns the empty contribution.
- **Per-plugin config rides `plugin_config`; capability flags ride
  `capability_flags`.** The feature-named config parameters (`skills_dir`,
  `memory_dir`, `instructions_*`, …) left the kernel signature; the host maps
  its unchanged public fields into `plugin_config = {"skills": {…}, "memory":
  {…}, "workspace": {…}}` and each pack parses only its own entry. The derived
  per-agent flags reach a pack as `capability_flags` (`"memory"`, `"browser"`,
  …); a pack **self-gates** on its context — inapplicable ⇒ the empty
  `PackContribution` — the kernel never gates for it.
- **The instructions/environment loader halves live in the `workspace` plugin
  (D10).** `load_instructions` / `discover_instructions` /
  `build_instructions_*` and `load_environment` + the git/date helpers moved out
  of `execution/instructions.py` / `execution/environment.py` into
  `workspace.impl`; the two residents' construction is now the `workspace`
  built-in's two `session_pack` factories (`instructions` band 400,
  `environment` band 500). The kernel keeps the resident **kit types**
  (`InstructionsKit` / `EnvironmentKit` / `MemoryIndexKit`); the typed record
  seams `record_instructions` / `record_environment` / `record_memory_index`
  are **gone** as of the kernel final form — each resident now records through
  its plugin's generic `init` hook + the scoped `SessionRecorder`
  (`docs/implementation-specs/archive/2026-07-30-kernel-final-form.md`; see the
  unified-context-supply Clarification 2026-07-30). The host reaches the loaders
  through the `noeta.client.parts.workspace_impl` doorway for the discovery
  hook's own path (record == compose stays true because both halves live in the
  one plugin).
- **External packs follow per-agent activation.** A third-party plugin's
  `session_pack` contributions fold on the `Client` into each agent's
  `activated_session_packs` and the host merges them **after** the built-in set;
  the builder loop interleaves them by declared priority. The extension proof
  (`tests/test_session_pack_extension.py`) is the goal test: a single-file
  plugin's pack tool reaches a built session, appends or interleaves at its
  band, and scopes to the activating agent only — **zero** kernel or SDK-host
  edits.
- **The per-feature factory injections the 2026-07-29 addendum listed are
  retired.** `fs`/`web`/`browser`/`app_tools_factory`, `memory_factory`, and the
  three kit fields are gone from `_BuildSpec` and `parts.py`, replaced by the
  single `parts.default_session_packs()` accessor; `mcp_tools_override` /
  `custom_tools` / `exec_env` and the generically-typed `guards_factory` /
  `default_policy_factory` / `base_reminders` injections stay (D1 / Non-goals).

## Addendum — 2026-07-30: durable storage backends ship as a declaration-only built-in

The storage-backend relocation
(`docs/implementation-specs/archive/2026-07-30-storage-backend-relocation.md`) moved the
durable backends out of the kernel wheel: `noeta.storage.{sqlite,postgres}`
became `noeta.builtins.storage.impl.{sqlite,postgres}` (noeta-sdk), taking the
`psycopg[binary]` dependency with them — the noeta-runtime wheel now carries no
database driver, and the first-party durable backends are co-located under
`noeta.builtins` like every other first-party implementation. The kernel keeps
the storage Protocols (`noeta.protocols`, unchanged), the InMemory reference
backend (`noeta.storage.memory`), and the public backend SPI
(`noeta.storage.spi`) fronting the shared domain rules.

- **The catalogue is eighteen built-ins.** The new `storage` built-in is a
  **declaration-only reference manifest with zero contributions** — the
  `providers` precedent, so the declaration-only-manifest pattern now has two
  members (`providers`, `storage`). The manifest documents the two backend
  `build_stack` factories for host discovery; the built-in is never activated,
  never loaded per-agent, and never enters `AgentSpec` identity. The single
  public doorway is `noeta.sdk.storage` (`build_storage_stack` /
  `open_storage_stack` + lazy class re-exports, on the `noeta.sdk.providers`
  lazy-import pattern). (The built-ins count above is updated inline.)
- **Alternative 5 is reaffirmed, not weakened.** Storage is still **not** a
  contribution surface: the EventLog / ContentStore / Dispatcher triple is the
  truth substrate every plugin guarantee stands on, a single host injection
  with no merge semantics — activation, collision, and ordering never apply to
  it, and the surface count does not change. This was a physical move of
  files, not trust; `HostConfig` remains the wiring path unchanged (a
  `storage_backend` string field on it was considered and rejected). A
  third-party backend implements the `noeta.protocols` storage Protocols plus
  the `noeta.storage.spi` domain rules, ships a `build_stack` factory, and the
  host injects the triple through `HostConfig` — no plugin machinery, no
  registration.

## Addendum — 2026-07-31: `merge_rule` → `activation_binding`, and the two process channels

The SDK-layer cleanup
(`docs/implementation-specs/2026-07-30-sdk-layer-cleanup.md`, D11) settled two
things this ADR left ambiguous about `SurfaceSpec`.

- **`merge_rule` is deleted — it was decoration.** No code ever read it: the
  append-vs-single behaviour is fully determined by `collision_key`
  (`single-valued` *is* the single-merge surface). A declared field promising
  a mechanism the loader does not implement is worse than no field, so it is
  gone rather than documented.
- **Its slot now holds `activation_binding`, which *is* read.** An
  identity-plane surface declares which `PluginActivation` channel its
  contribution feeds (`tool` / `agent` / `content_kind` / `prompt_fragment` /
  `policy`, or `elsewhere` when a per-agent projection carries it, as
  `control_tool` does). `PluginSet.identity_activations()` dispatches on that
  table instead of matching surface names, which is what finally makes the
  **identity** projection as surface-agnostic as the rest of the loader: a
  host-registered identity surface reaches `compile_options` with no loader
  edit. The binding is required for `plane="identity"` and rejected for every
  other plane, checked in `SurfaceSpec.__post_init__` — the
  `content_kind`-went-missing failure now fails at registration, before any
  plugin loads. Every enum field is validated there too, so an argument left
  in the old `merge_rule` position raises instead of silently landing in
  `ordering`.
- **The wiring plane has exactly two process-wide channels.** `guard` and
  `observer` are the only process-scoped seams `Client` wires, so a
  process-scoped wiring surface beyond those two is **refused** by
  `process_hooks()`. This ADR's "governance is process-wide authority" rule
  stands; what is new is that the set is derived from the registry and a
  surface it cannot route is an error, not a value filed under `guards` — a
  non-`Guard` value reaching the engine turns a build-time configuration
  error into a crash on the first tool call. A host surface that is not
  governance takes a per-agent scope.
