# SDK extensibility redesign: manifest plugins, surface registry, activation

> **Status: Shipped** — landed in `6d01b11` (feat!: rebuild the plugin
> mechanism); the durable decisions live in
> [plugin-contribution-bundles.md](../../adr/plugin-contribution-bundles.md).

Supersedes the *mechanism* design of
[2026-07-25-plugin-architecture.md](archive/2026-07-25-plugin-architecture.md)
(whose M1+M2 shipped as the 0.4.0 local release). Nothing of 0.4.0 is
published — no PyPI, no tag — so **no backward compatibility is owed**; this
redesign replaces the mechanism outright. Durable decisions land by rewriting
[plugin-contribution-bundles.md](../adr/plugin-contribution-bundles.md) in M5.

## Goal

Rebuild the plugin mechanism as **manifest-declared contribution packages over
a surface registry**, with a **host-level load / agent-level activation**
split, open **six new extension surfaces** (`reminder_provider`, `reminder`,
`tool_result_transform`, `policy`, `prompt_fragment`, `sandbox_provider`), and
re-express noeta's built-in capabilities as **built-in plugins** loaded through
the same path — so third parties extend noeta (including system-reminder /
context injection) with distributable pip packages, while replay, the
stable-prefix KV cache, and multi-tenant governance remain **structural**
guarantees rather than author discipline.

## Non-goals

- **No imperative in-loop hooks** — no `on("context")` message rewriting, no
  `setActiveTools()` mid-session mutation. Re-affirmed against pi's model
  (whose `ExtensionAPI` lives in its *app* package, not its engine package).
  Escape routes for the declined power: wire-level rewriting → a wrapping
  `provider` contribution; custom history compression → a future *recorded*
  compaction seam; dynamic tool sets → a future fold-modeled activation
  design (like `active_skills`).
- **Storage is never a plugin.** The EventLog/ContentStore/Dispatcher triple
  is the truth substrate every plugin guarantee stands on (trust
  bootstrapping), and it is a single host injection with no merge semantics —
  the plugin mechanism adds nothing. Third-party backends are ordinary
  packages implementing the `noeta.storage` protocols, wired via `HostConfig`.
- **Control tools are runtime mechanism, not contributions.** `todo_write` /
  `skill` / `ask_user_question` / the subagent dispatch tool are renderings of
  kernel Decision variants; activation gates their *visibility*, plugins never
  contribute them.
- **Composer wholesale replacement stays closed**; Engine loop, Worker/Lease/
  Dispatcher untouched.
- **No physical wheel split** of first-party capabilities
  ([package-layout.md](../adr/package-layout.md) stands): built-in plugins are
  thin *declarations*; implementations stay in their import-linter bands.
- **No app-plane API in this repo** — each host owns its app-plane surfaces in
  its own repository; hosts may *register* those surfaces into this
  mechanism's registry (D2) but their contracts are host property.
- No plugin registry/marketplace service; pip/git remains the registry.

## Context

The 0.4.0 mechanism (typed contribution bundles: `noeta_plugin(api)` factory,
entry points, trust store, deterministic merge) is sound in its *semantics* —
declarative, identity-bearing, collision-loud — and live-verified (S12,
2026-07-28 handoff). Its *shape* has three limits this redesign removes:

1. **One hardcoded method per surface** (`add_tool` / `add_guard` / …): every
   new surface is an SDK code change; hosts cannot define surfaces.
2. **Discovery requires execution**: knowing what a plugin contributes means
   running its factory; the `enabled` gate rests on an `ast` trick.
3. **`Capabilities` duplicates the concept**: per-agent feature gating
   (`memory` / `browser` / `skill_invocation`) is "activate a built-in
   feature bundle" in disguise — a second mechanism for the same idea.

Owner decisions from the 2026-07-28 design interview: rebuild the mechanism
(manifest + unified registry — the declarative form, imperative hooks
explicitly declined after the pi comparison); open reminder tracks A+B; open
`policy` / `sandbox_provider` / `tool_result_transform` / `prompt_fragment`;
built-ins live in a dedicated directory and ride the same loader; load/activate
replaces `Capabilities`; governance contributions do not follow activation;
storage stays out.

Constraints honored: [single-writer-invariant.md](../adr/single-writer-invariant.md),
[guard-observer-hooks.md](../adr/guard-observer-hooks.md) (two hook roles; the
tool-result transform is a ToolRuntime *stage*, not a third role), the locked
`ThreeSegmentComposer` (registry hooks only), `Options` (identity) vs
`HostConfig` (host wiring), stable-prefix reproducibility.

## Decisions

### D1 — Plugin unit and manifest

A **Plugin** is a package (or a single `.py` file for local/dev use) carrying a
**static manifest**:

- **Distributed form**: `[tool.noeta]` in `pyproject.toml`, mirrored into the
  wheel as package data (`noeta-plugin.toml`) located via the distribution
  metadata — the loader reads it **without importing any plugin code**.
- **Manifest fields**: `name`, `requires-noeta` (version range), optional
  `config-schema`, and a list of contributions: `surface` + (`ref` import
  string for code | `path` for resources) + surface-specific params (e.g.
  `seams`, `priority`).
- **Single-file form** (local dirs only): decorators (`@plugin.tool`,
  `@plugin.reminder_provider(...)`) act as manifest sugar. Acceptable because
  local files pass an explicit trust gate anyway; a packaging check derives
  and verifies the TOML from the decorators at publish time (shipped as
  `python -m noeta.sdk.plugin_check` — **no console script**, per "there is no
  operator CLI").

### D2 — Surface registry (the generality mechanism)

The loader is **surface-agnostic**. It consults one registry:

```
surface name → SurfaceSpec(
    plane            = identity | wiring | host,
    activation_scope = per-agent | process | host-wired,   # see D6
    validator        = what a legal contribution value is,
    collision_key    = name | kind | alias | single-valued | none,
    merge_rule       = append | single | dict-merge,
    ordering         = sorted(plugin, name) | priority-int,
)
```

The SDK registers the standard set (D3). A host may register additional
(app-plane) surfaces **before** load; host-plane contributions are validated
and collision-checked by the same pipeline and handed to the host, never
entering `Options`. Adding a future surface = registering one `SurfaceSpec`;
the loader does not change.

### D3 — Standard surface catalog

| Surface | Plane | Scope (D6) | Cardinality | Notes |
|---|---|---|---|---|
| `tool` | identity | per-agent | multi, name-keyed | includes tool packs |
| `agent` | identity | per-agent | multi, name-keyed | `AgentDefinition` |
| `content_kind` | identity | per-agent | multi, kind-keyed | track C (exists) |
| `prompt_fragment` ★ | identity | per-agent | multi, name-keyed | appended after the preset prompt, sorted `(plugin, name)` |
| `policy` ★ | identity | per-agent | **single** | collision with base or another active plugin = error |
| `guard` | wiring | **process** | multi | governance — see D6 |
| `observer` | wiring | **process** | multi | governance — see D6 |
| `provider` | wiring | host-wired | **single** | `Options.provider` collision = error; also the wire-level escape hatch |
| `reminder_provider` ★ | wiring | per-agent | multi, name-keyed | track A, D7 |
| `reminder` ★ | wiring | per-agent | multi, name-keyed | track B, D8 |
| `tool_result_transform` ★ | wiring | per-agent | multi, name-keyed | D9 |
| `mcp_server` | host | host-wired | multi, alias-keyed | |
| `skills` | host | host-wired | multi, path | resource-only plugins |
| `sandbox_provider` ★ | host | host-wired | multi, name-keyed; host selects one | D10 |

★ = new in this redesign. Host-defined surfaces extend this table per host.

### D4 — Sources and the load pipeline

Five sources; discovery order **never** affects the result (only error
attribution):

| # | Source | Gate |
|---|---|---|
| 0 | built-in plugins (`noeta.builtins`, D11) | on by default; host may disable individually |
| 1 | entry points (`noeta.plugins` group) | `enabled` allow-list |
| 2 | explicit modules / file paths | caller-specified = authorized |
| 3 | `~/.noeta/plugins/` | user's own machine = trusted |
| 4 | workspace `.noeta/plugins/` | trust store (untrusted dir → loud warning + skip) |

Pipeline (every candidate): read manifest (zero code execution) → `enabled`
gate **before any import** → trust gate (source 4) → resolve `ref`s →
validate per `SurfaceSpec` → collision check → deterministic merge sorted by
`(plugin name, contribution name)`. Collisions — including cross-source
duplicate plugin names and collisions with base `Options` content — are
**errors naming both sides; there is no override**. All load faults fail the
client build, never a mid-session turn.

### D5 — Two-level model: load, then activate

- **Load (host level)**: `load_plugins(...) -> PluginSet` — which plugin code
  is in the process. `PluginSet` is listable/auditable without executing
  plugin code.
- **Activate (agent level)**: `Options.plugins: list[str]` and
  `AgentDefinition.plugins` — which loaded plugins *this agent* uses.
  Activation enters `AgentSpec` identity (as `Capabilities` did).
- `Client(options, plugins=<PluginSet>)`; activation names must exist in the
  loaded set or compile fails loudly.
- **`Capabilities` is retired.** `memory=True` becomes `plugins=["memory"]`;
  `browser`, `skill_invocation` likewise. Presets declare their default
  activation sets; a `DEFAULT_PLUGINS` constant makes a bare `Options()`
  compile **byte-identical** to today's spec (fs/web tools on, memory/browser
  off — exact set pinned by the parity test, M2).

### D6 — Effect scoping (the one deliberate asymmetry)

| Surfaces | Rule |
|---|---|
| `tool` `agent` `content_kind` `prompt_fragment` `policy` `reminder_provider` `reminder` `tool_result_transform` | **follow per-agent activation** (feature semantics) |
| `guard` `observer` | **loaded ⇒ in force for every agent in the process.** Governance is operator authority; an agent author must not be able to opt out of compliance interception or audit by omitting an activation. |
| `provider` `sandbox_provider` `mcp_server` `skills` | host wiring; existing per-agent override semantics (`Options.provider` etc.) unchanged |

### D7 — Reminder track A: `reminder_provider` (recorded injection)

- **Seams (v1)**: `turn_intake` (user message being recorded), `task_seed`
  (task creation). `task_wake` / `subtask_result` are deferred until a real
  tenant demands them.
- **Contract**: the provider receives a narrow read-only `RecallView`
  (task id, incoming message, a `TaskState` projection, workspace path) and
  returns zero or more `Reminder(text, origin)` with `origin ∈ {system,
  memory}`. The provider **may be impure** (query a vector DB, an external
  system) because its output is **recorded** through the Engine's sole
  origin-writer seam. Resume/replay folds the ledger and **never re-invokes
  providers**.
- Multiple providers on one seam run in `(plugin, name)` order. A provider
  raise fails the turn loudly (a plugin that prefers degradation catches
  internally); no silent skips.
- Built-in memory auto-recall (`append_user_message_with_recall`) is
  re-expressed as the first built-in tenant. This is also the missing seam
  for RAG-backed memory plugins — one design closes both.

### D8 — Reminder track B: `reminder` (compose-time, pure)

- A `reminder` contribution is `(name, priority: int, render)` where `render`
  is a **pure function of a narrow folded-state projection** returning
  `str | None`, rendered at the **tail of the dynamic suffix** (adapter wraps
  in `<system-reminder>`). The stable prefix is untouched by construction.
- Ordering: integer `priority`, ties broken by `(plugin, name)` — the same
  single-integer precedent as guard-observer-hooks.
- The three composer built-ins (unfinished-todos, delegation nudge, read
  suggestion) migrate as the first tenants, priorities chosen to keep today's
  output byte-identical.
- Determinism of third-party render functions is a documented contract — the
  same trust class as existing `ContentKindSpec` renderers (accepted).

### D9 — `tool_result_transform` (ToolRuntime pipeline stage)

Pure `ToolResult → ToolResult` transforms applied **inside the tool execution
boundary, before recording** — the transformed output *is* the recorded
output (redaction means the secret never reaches the ledger). Ordering as D8.
Not a third hook role; Guard/Observer stays exactly two
(guard-observer-hooks.md unchanged in substance).

### D10 — `policy`, `prompt_fragment`, `sandbox_provider`

- **`policy`**: single-valued per agent; a base `Options.policy` plus an
  active plugin policy, or two active plugin policies, is a loud collision
  (same rule as `provider`).
- **`prompt_fragment`**: appended after the preset/system prompt, sorted
  `(plugin, name)`; enters identity. Precedent: `MEMORY_POLICY_PROMPT` (which
  migrates onto this surface inside the `memory` built-in).
- **`sandbox_provider`**: host-plane; the host selects which loaded provider
  to wire (selection is host wiring, **never** agent identity). The
  retirement-slated AIO adapters migrate as the first built-in. Secrets never
  appear in manifest, config, or ledger — `SandboxAuth` stays a live object.

### D11 — Built-in plugins (`noeta/builtins/`)

- New top-of-stack directory in **noeta-sdk**, alongside `noeta.presets`
  (import-linter: a new band `noeta.builtins | noeta.presets`; nothing below
  imports it; the loader reaches it via `ref` strings — dynamic import, no
  static edge).
- Each built-in is a **thin declaration** (manifest + refs); implementations
  stay in their runtime bands untouched. Initial set: `fs`, `web`, `browser`,
  `memory`, `skills`, `reminders`, `governance` (the 5 default hooks),
  `providers` (the 3 LLM adapters), `presets` (the official agents).
- Built-ins ride the **identical** loader/validation/merge path as external
  plugins — noeta is its own first plugin author; every surface has a
  built-in reference implementation.
- Adding a first-party capability = adding a directory here (+ a
  `SurfaceSpec` registration only if it needs a genuinely new surface).

### D12 — Vocabulary (CONTEXT.md changes, executed in M5)

**Plugin** (redefined: manifest-declared contribution package), **PluginSet**
(the loaded, host-level set), **activation** (per-agent selection, in
identity), **Surface** / **SurfaceSpec** (the registry), **Built-in plugin**;
**Capabilities retired**; **Reminder tracks A/B/C** named. `guard-observer-
hooks.md` gains an effect-domain addendum (D6); `plugin-contribution-
bundles.md` is rewritten around D1–D6.

## Plan

Dependency order: M1 → M2 → (M3 ∥ M4) → M5. M1 and M2 land **together**
(removing `Capabilities` without the parity net breaks compile).

- [x] **M1 — Mechanism core**: `SurfaceSpec` registry; manifest schema +
      no-execution reader (package-data + single-file decorator sugar);
      five-source loader with gates; `PluginSet`; deterministic merge over
      the registry; `Options.plugins` / `AgentDefinition.plugins` +
      activation-aware compile; `Capabilities` removal.
- [x] **M2 — Built-ins + parity**: `noeta/builtins/` tree and declarations;
      `DEFAULT_PLUGINS`; preset activation sets; **byte-identical parity
      test** (bare `Options()` and every preset compile to today's
      `AgentSpec`); import-linter band + contract.
- [x] **M3 — Reminder tracks**: engine seams `turn_intake` / `task_seed` +
      `RecallView` + recording path (characterization test pins current
      attachment/goal-origin ordering first); composer reminder registry +
      migrate the three built-ins (byte-identical); memory recall as built-in
      `reminder_provider`.
- [x] **M4 — New surfaces**: `tool_result_transform` stage in ToolRuntime;
      `policy`; `prompt_fragment` (+ `MEMORY_POLICY_PROMPT` migration);
      `sandbox_provider` (+ AIO built-in).
- [x] **M5 — Docs + examples**: rewrite the plugin ADR; CONTEXT.md vocabulary
      (D12); guard-observer addendum; `docs/reference/plugins.md` +
      how-to; migrate `examples/plugins/` + add one exemplar per new surface
      (a RAG-recall example for track A in particular); `plugin_check`
      packaging verifier.

## Acceptance criteria

1. Bare `query()` / `Options()` / every preset compiles to a **byte-identical
   `AgentSpec`** versus pre-redesign (parity test).
2. `PluginSet` lists every contribution of an installed plugin **without
   executing plugin code** (import-sentinel test).
3. A non-`enabled` entry-point plugin is **never imported**.
4. Any collision (two plugins; plugin vs base; cross-source duplicate name;
   second `policy`/`provider`) fails at build **naming both sides**; no
   override path exists.
5. Same loaded set + same activation ⇒ byte-identical `AgentSpec` under any
   discovery order.
6. Per-agent activation: an agent without `memory` active carries no memory
   tools, no memory resident, no memory prompt fragment; a sibling with it
   carries all three.
7. A loaded `guard` applies to an agent that did **not** activate its plugin.
8. Track A: the reminder is in the ledger with its origin; resume replays it
   **without re-invoking the provider** (call-count test); a provider raise
   fails the turn loudly.
9. Track B: pre/post-migration composer output byte-identical for the three
   built-ins; a plugin reminder renders at the dynamic-suffix tail; the
   stable-prefix hash is unchanged across steps with reminders active.
10. `tool_result_transform`: the ledger contains only the transformed result
    (redaction leaves no secret anywhere durable).
11. A `sandbox_provider` plugin selected by the host provisions and
    re-attaches across resume; no secret in manifest/config/ledger.
12. `make check` green, including the new import-linter contract.

## Risks

- **Parity drift** while removing `Capabilities` — mitigated by writing the
  parity test before the removal (M2 ordering).
- **Manifest-as-package-data discovery** edge cases (zip / editable installs)
  — verify `importlib` resource paths for both install modes early in M1.
- **Track-A seam ordering** interacts with the existing attachment /
  `goal_origin` recording order — characterization test first (M3).
- **Scope**: large change set; milestones are independently landable except
  M1+M2 (must pair). No publish happens mid-redesign (nothing is on PyPI, so
  there is no partial-release hazard).
- Third-party render-function determinism (tracks B/C) is contractual, not
  enforceable — accepted, same class as today's `ContentKindSpec`.

## Progress log

- 2026-07-28 — Spec written after the design interview converged (mechanism
  rebuild chosen over surface-only additions; imperative hooks declined after
  the pi comparison; load/activate unification of `Capabilities`; storage
  ruled out of the plugin mechanism).
- 2026-07-29 — M1–M5 implemented (staged subagent workflow: baseline
  parity/characterization net → mechanism core → builtins + activation
  switch-over → reminder tracks A/B → four new surfaces → docs/examples).
  Acceptance audit: 12/12 criteria pass; `make check` green.
- 2026-07-29 — Deferred teardown completed: old `PluginAPI`/`merge_plugins`
  path deleted; `Options.capabilities` / `AgentDefinition.capabilities`
  authoring fields removed (`AgentSpec.capabilities` kept as the folded
  identity carrier per D5); `tests/test_plugins.py` retired (2 trust-store
  cases ported). Surface+docs audit findings (stale `capabilities=` mentions
  in CONTEXT.md / two ADRs / two how-tos, stale loader docstring) fixed.
  Gates green: parity 5/5 unchanged, `make check` EXIT=0, import-linter 13/13.
- 2026-07-29 — Code-review pass over the whole change set; 15 findings fixed.
  The theme was **declared but not wired**: several surfaces were registered,
  validated, merged and documented while nothing on the `Client` path carried
  them into the engine. Now wired end-to-end and pinned by
  `tests/test_plugin_wiring_contract.py`:
  * `reminder` (track B) — per-agent `SdkHost.extra_reminders` →
    `build_session_inputs(extra_reminders=…)`;
  * `reminder_provider` (track A) — per-agent, per-seam
    `SdkHost.reminder_providers` → the new `intake_reminder_providers` seam,
    composed with the built-in memory recall by `memory.intake_providers` so
    both intake call sites share one rule;
  * `content_kind` — `PluginActivation.content_kinds` →
    `SdkHost.activated_content_kinds` (it passed the plane filter and then fell
    through every branch; an unhandled identity surface now raises);
  * `PluginSet.merged()` — the collision pass now runs on the real build path,
    so "no override" is enforced where it matters, not only in the loader.

  Also: a raising `tool_result_transform` no longer strands the ledger (it
  collapses to a failed result carrying no payload); `tool_result_transforms`
  are honoured with an empty tool set and refused loudly against an injected
  `ToolRuntime`; plugin tool / child-agent collisions with the base recipe are
  errors naming both sides; `delegation` joins the activation vocabulary (the
  successor to `AgentDefinition.capabilities=Capabilities(delegation=True)`,
  which had become unreachable); resolution is activation-scoped, manifest-
  filtered and memoised, so loading a plugin no agent activates no longer runs
  its module body; dotted `pkg.mod.attr` refs resolve; `prompt_fragment` text
  survives `plugin_check --emit`; a malformed shipped manifest is a `FAIL` line
  rather than a traceback; `noeta.builtins` asserts the catalogue is a subset of
  the activation vocabulary so the two hardcoded lists cannot drift.
  `make check` EXIT=0, coverage 87.6%, import-linter 13/13.

- **2026-07-29 — built-ins restructured to one directory per built-in (D11
  literal).** The single `noeta/builtins/_catalog.py` is replaced by ten
  subpackages (`noeta/builtins/{fs,web,memory,browser,skills,reminders,
  governance,providers,sandbox,presets}/`), each holding its thin `MANIFEST`
  declaration, plus a package-private `_declare.py` helper; the package
  `__init__.py` aggregates them and keeps the same three-name public surface
  (`builtin_manifests` / `BUILTIN_PLUGIN_NAMES` /
  `assert_activation_vocabulary`), the same catalogue order, and the
  import-time vocabulary assertion. "Adding a first-party capability = adding
  a directory here" now holds literally, as D11's prose states. Doc references
  updated (en/zh reference + how-to). No behaviour change.
