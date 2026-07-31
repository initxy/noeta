# Plugins are manifest-declared contribution packages over a surface registry

## Context

Noeta's extension points are typed seams: Tool, LLMProvider, Policy, Guard,
Observer and `ContentKindSpec` on `Options`, storage and the other host
injections on `HostConfig`. What they do not supply is a **unit of packaging and
discovery** — a way to ship a coherent bundle of extensions, find it on a
machine, and say which agents use it.

Three engine invariants constrain any answer. **Event-sourced replay**: whatever
shapes a turn must be reconstructible from the ledger, so an extension's effect
has to compile into durable identity rather than run live inside the loop. **The
stable-prefix KV cache**: anything that rewrites context per call destroys cache
reuse. **Multi-tenant operator authority**: the party installing code and the
party authoring an agent are different parties with different powers.

## Decision

### The plugin unit and its manifest

A **Plugin** is a package — or a single `.py` file for local and development
use — carrying a **static manifest** of inert data: `name`, `requires-noeta`, an
optional `config-schema`, and a tuple of contributions. Each contribution is a
`surface` plus a `ref` (a `module:qualname` import string) **or** a `path`, plus
surface-specific params such as `priority`, `seams` or `alias`. The `ref` is a
string; nothing in the manifest layer imports it. A `(surface, name)` pair is
unique within one manifest — it is the collision and ordering key every later
projection is built on.

The distributed form is `[tool.noeta]` in `pyproject.toml`, mirrored into the
wheel as package data (`noeta-plugin.toml`). `noeta.client.plugin_manifest` reads
it **without importing any plugin code**: the manifest listed in the
distribution's RECORD is read straight off disk, and an editable install (where
RECORD may omit package data) falls back to `importlib.util.find_spec`, which
locates a package directory without executing its body.

The single-file form is a module-level `PluginBuilder` whose decorator sugar
(`@plugin.tool`, `@plugin.reminder(priority=…)`, …) *is* the manifest. Reading it
executes the file — acceptable because local files pass an explicit trust
gate — and a module-level `noeta_plugin_name` string or
`plugin = PluginBuilder("…")` literal is extracted with `ast`, so the `enabled`
allowlist gates the plugin *before* its body runs. `noeta.sdk.plugin_check`
derives and verifies the TOML from the decorators as a `python -m` entry; there
is no operator CLI.

### The surface registry

The loader is **surface-agnostic**: it consults one `SurfaceRegistry`
(name → `SurfaceSpec`, in `noeta.client.surfaces`) and nothing else.

| Field | Meaning |
|---|---|
| `plane` | `identity` \| `wiring` \| `host` — mirrors the `Options` / `HostConfig` split |
| `activation_scope` | `per-agent` \| `process` \| `host-wired` — how the effect is scoped |
| `validator` | called on a **resolved** value; raises when it is not a legal member of the surface |
| `collision_key` | `name` \| `kind` \| `alias` \| `single-valued` \| `none` — the namespace two contributions clash in |
| `ordering` | `sorted` = `(plugin, name)`; `priority` = an integer param, ties broken by `(plugin, name)` |
| `activation_binding` | identity-plane only — which `PluginActivation` channel the contribution feeds |

`standard_registry()` returns a fresh registry seeded with the standard
catalogue. A host that owns app-plane surfaces takes a copy and registers its own
**before** load; the same validation, collision and ordering pipeline runs over
host-plane contributions and hands them to the host — they never enter `Options`.
Adding a surface is registering one `SurfaceSpec`; the loader does not change, and
a duplicate surface name is refused with no override.

Every enum field and the binding rule are checked at construction. An
identity-plane surface **must** declare a binding — `tool`, `agent`,
`content_kind`, `prompt_fragment`, `policy`, or `elsewhere` when a per-agent
projection carries it — and no other plane may: an identity contribution with no
binding is silently dropped between resolution and compilation, compiling the
wrong identity.

### The standard surface catalogue

| Surface | Plane | Scope | Cardinality |
|---|---|---|---|
| `tool` | identity | per-agent | multi, name-keyed |
| `agent` | identity | per-agent | multi, name-keyed (`AgentDefinition`) |
| `content_kind` | identity | per-agent | multi, kind-keyed |
| `prompt_fragment` | identity | per-agent | multi, name-keyed; appended after the preset prompt |
| `policy` | identity | per-agent | **single-valued** |
| `control_tool` | identity | per-agent | multi, name-keyed, `priority` |
| `guard` | wiring | **process** | multi |
| `observer` | wiring | **process** | multi |
| `provider` | wiring | host-wired | **single-valued** |
| `reminder_provider` | wiring | per-agent | multi, name-keyed |
| `reminder` | wiring | per-agent | multi, name-keyed, `priority` |
| `tool_result_transform` | wiring | per-agent | multi, name-keyed, `priority` |
| `session_pack` | wiring | per-agent | multi, name-keyed, `priority` |
| `mcp_server` | host | host-wired | multi, alias-keyed |
| `skills` | host | host-wired | multi, path |
| `sandbox_provider` | host | host-wired | multi, name-keyed; the host selects one |

`session_pack` and `control_tool` carry construction factories the kernel builder
runs in priority-ordered loops; a factory self-gates on its build context, and
the priority bands are locked by goldens because construction order feeds the
stable prefix.

### Sources and the load pipeline

Five sources; discovery order affects only error attribution, never the result:

| # | Source | Gate |
|---|---|---|
| 0 | built-in plugins (`noeta.builtins`) | on by default; a host may disable individually |
| 1 | entry points (the `noeta.plugins` group) | `enabled` allowlist |
| 2 | explicit modules / file paths | caller-specified = authorized |
| 3 | `~/.noeta/plugins/` | the user's own machine = trusted |
| 4 | workspace `.noeta/plugins/` | trust store; an untrusted directory is skipped with a loud warning |

The pipeline for every candidate: read the manifest (zero code execution for the
package and TOML forms) → apply the `enabled` gate **before any import** → apply
the trust gate for source 4 → resolve `ref`s → validate per `SurfaceSpec` → check
collisions → merge deterministically by `(plugin, contribution)` name.
Collisions — including a duplicate plugin name across two sources, and two active
contributions to a single-valued surface — are **errors naming both sides; there
is no override**. Any load fault raises `PluginError` naming the plugin and
**fails the client build, never a mid-session turn**; the untrusted-workspace
skip is the only non-raising case, and it warns. A per-name disable the compiled
agent cannot honour is likewise refused: `react` supplies the default decision
policy every compiled `AgentSpec` pins as its policy identity, so it is
replaceable — by activating a plugin contributing the `policy` surface — but not
removable.

### Load, then activate

Loading and per-agent selection are one axis with two levels. **Load** is host
level: `load_plugins(...)` returns a `PluginSet` deciding which plugin code is in
the process, listable and collision-checkable **without executing plugin code**;
resolution is the single boundary that imports a `ref`, called at the client
build and never on a turn. **Activate** is agent level: `Options.plugins` and
`AgentDefinition.plugins` decide which loaded plugins an agent uses, and a name
that is neither a recognised built-in nor a plugin in the loaded set fails
compilation loudly.

Activation *is* agent identity: `AgentSpec` carries the sorted `plugins` tuple
plus `spawnable`, and a feature is active precisely when its name is in that
tuple — there is no second set of feature booleans to keep in agreement. Both
members of `DEFAULT_PLUGINS = ("fs", "web")` are identity-inert, so a bare
`Options()` compiles to an `AgentSpec` a parity golden pins byte-for-byte.

### Effect scoping — the one deliberate asymmetry

Effect follows the surface's `activation_scope`, not a uniform rule:

| Surfaces | Rule |
|---|---|
| `tool` `agent` `content_kind` `prompt_fragment` `policy` `control_tool` `reminder_provider` `reminder` `tool_result_transform` `session_pack` | **follow per-agent activation** — a sibling agent that did not activate the plugin carries none of it |
| `guard` `observer` | **loaded ⇒ in force for every agent in the process** — governance is operator authority; an agent author must not opt out of compliance interception or audit by omitting an activation |
| `provider` `sandbox_provider` `mcp_server` `skills` | host wiring; the host selects one, per-agent override unchanged |

This is why `guard` and `observer` sit on the wiring plane with process scope
rather than per-agent: the scope field encodes that governance is not an
agent-author choice. The process-wide channels are derived from the registry and
there are exactly two, because the `Client` wires guards and observers into two
different runtime seams; a third process-scoped wiring surface is **refused
loudly** rather than filed under guards, where an unroutable value would turn a
build-time configuration error into a crash on the first tool call. A host
surface that is not governance takes a per-agent scope.

### Built-in plugins ride the same path

Noeta is its own first plugin author. `noeta.builtins` is a top-of-stack band
beside `noeta.presets`, one directory per built-in holding its manifest **and**
its implementation: the package `__init__` is the zero-execution manifest
declaration, the sibling `impl/` package is the code its `ref`s point at, so
listing a built-in's contributions runs zero runtime code. The bare-`Options()`
defaults — the fs/web tool set, the guards, the compose-time reminders, the
provider facts — come from resolving those manifests through the
`noeta.client.parts` accessors at client build; there is no static default table,
and the kernel builder is injection-only, failing loudly when a part is absent.

Nothing statically imports `noeta.builtins`: the loader reaches it by dynamic
import, and `.importlinter` enforces the absence of that static edge universally,
from every band including the kernel. Two of the eighteen built-ins —
`providers` and `storage` — are declaration-only reference manifests with zero
contributions, giving the adapter and backend implementations a home; they are
never activated and never enter agent identity. Adding a first-party capability
is adding a directory here, plus a `SurfaceSpec` registration only for a
genuinely new surface.

## Rationale

- **Generic without granting the engine new power.** A new surface is one
  `SurfaceSpec`, and a host can define its own app-plane surfaces — yet every
  contribution compiles into `AgentSpec` identity or is handed to host wiring, so
  the engine gains zero new consistency obligations. Plugins substitute nothing;
  they populate.
- **Discovery is execution-free by construction.** The manifest is static data
  read without importing the `ref`, so the `enabled` gate applies before any
  plugin code runs: code from an untrusted source is never executed to find out
  what it would have contributed.
- **One axis, not two.** "Which plugins does this agent use" answers both "which
  built-in features are on" and "which third-party packages contribute", with a
  single failure mode (unknown name → loud) and a single identity path.
- **Determinism survives.** Contributions compile into `AgentSpec` identity and
  merge order is `(plugin, name)` over static manifests, so two hosts with the
  same loaded set and activation produce byte-identical agents under any
  discovery order — replay, resume and KV-cache reuse behave exactly as with
  hand-written `Options`.
- **Authoring stays ergonomic.** A single file is a plugin and decorators are the
  manifest, while the contract underneath stays declarative — an author cannot
  break invariants they never learned about.

## Alternatives considered

1. **An imperative in-loop event bus** — `on("tool_call")`, `on("tool_result")`,
   `setActiveTools()`. Rejected: it reopens the mutator role that
   `guard-observer-hooks.md` rules out, turns replay safety into a documentation
   promise, hands session-time mutation power to third-party code in a
   multi-tenant process, and rewrites context per call against the stable-prefix
   cache. The power it offers has bounded, recorded escape routes instead:
   wire-level rewriting through a wrapping `provider`, custom history compaction
   through a recorded compaction seam, dynamic tool sets through fold-modeled
   activation — none of them an in-loop mutation hook.
2. **A context-filter hook.** Rejected: it conflicts directly with the locked
   composer and the stable-prefix constraint. Structured injection is the
   sanctioned path, in three tracks — recorded (`reminder_provider`),
   compose-time (`reminder`) and resident (`content_kind`) — none of which
   perturbs the stable prefix.
3. **A fixed-method plugin API, one `add_<surface>` method per surface.**
   Rejected: every new surface becomes an SDK code change, a host cannot define a
   surface of its own on a closed method set, and discovery requires executing
   the plugin's factory to learn what it contributes.
4. **Config-only plugins — declarative manifests with no code at all.** Rejected
   as the *whole* mechanism: a manifest cannot carry a Guard, a provider or a
   transform, so a `ref` must resolve to real code at the build boundary. MCP
   connectors and skills serve the no-code niche.
5. **Storage as a plugin surface.** Rejected on principle: the EventLog /
   ContentStore / Dispatcher triple is the truth substrate every plugin guarantee
   stands on, so it cannot be bootstrapped by the mechanism it underwrites. It is
   a single all-or-none host injection with no merge semantics — activation,
   collision and ordering add nothing. A third-party backend is an ordinary
   package implementing the `noeta.protocols` storage Protocols plus the domain
   rules in `noeta.storage.spi`, shipping a stack factory the host wires through
   `HostConfig`.
6. **An app-plane plugin API in this repository.** Rejected: each host owns its
   app-plane surfaces — routers, channels, schedules, commands — in its own
   repository. A host may *register* those surfaces into this registry, but their
   contracts are host property and never enter `AgentSpec` identity, on the same
   reasoning that separates `HostConfig` from `Options`.
7. **A `merge_rule` field on `SurfaceSpec`** declaring append versus single merge.
   Rejected: `collision_key` already determines it — `single-valued` *is* the
   single-merge surface — so the field would promise a mechanism the loader does
   not implement, and a declared field nothing reads is worse than none.
8. **A plugin registry or marketplace service, or a separate plugin repository.**
   Rejected: pip and git are the registry, and first-party examples live in-repo
   because a worked example corpus is the most effective plugin documentation.
9. **User-uploaded plugin code in a multi-tenant server.** Rejected: that is
   arbitrary code running across tenants. Users extend through MCP connectors and
   skills; operators extend through plugins.

## Consequences

- The mechanism lives in noeta-sdk: the surface registry
  (`noeta.client.surfaces`), the manifest schema and reader
  (`noeta.client.plugin_manifest`), the trust store and error surface
  (`noeta.client.plugins`), the loader and `PluginSet` (`noeta.client.plugin_set`),
  the activation vocabulary and compile wiring (`noeta.client.options`), and the
  catalogue with its accessors (`noeta.builtins`, `noeta.client.parts`).
  Contributions reach the runtime only as construction inputs, never as in-loop
  callbacks.
- `.importlinter` carries the contract that nothing statically imports
  `noeta.builtins`, alongside the rule keeping SDK core from statically importing
  `noeta.presets`.
- Changing the loaded-and-activated plugin set changes `AgentSpec` identity, and
  with it the KV-cache prefix.
- Designing a new extension surface includes choosing its `plane`,
  `activation_scope` and `activation_binding`.
