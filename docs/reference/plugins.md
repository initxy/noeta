# Plugins reference

The plugin mechanism — **manifest-declared contribution packages over a surface
registry**, with a **host-level load / agent-level activation** split. A plugin
carries a static manifest listing its contributions; loading reads those
manifests (no plugin code runs) into a `PluginSet`; an agent then *activates* the
plugins it uses through `Options.plugins`. Every symbol below is re-exported
through `noeta.sdk`; source of truth:
`packages/noeta-sdk/noeta/client/{plugin_manifest,surfaces,plugin_set}.py`.

```python
from noeta.sdk import (
    # the manifest + the single-file builder
    PluginManifest, ManifestContribution, PluginBuilder,
    # the surface registry (the generality mechanism)
    SurfaceSpec, SurfaceRegistry, standard_registry,
    # the loader + the loaded set
    load_plugin_set, PluginSet,
    # activation
    PluginActivation, DEFAULT_PLUGINS,
    # trust + errors
    grant_trust, is_trusted, PluginError,
)
```

> `load_plugin_set` is the `noeta.sdk` name for `noeta.client.plugin_set.load_plugins`
> (the internal function is `load_plugins`; it is re-exported under the
> `load_plugin_set` alias so it does not collide with the retiring 0.4.0
> `load_plugins`, see [The retired bundle path](#the-retired-bundle-path)).

> Line numbers are omitted throughout — they drift on every edit. The module path
> plus the member name is the stable coordinate.

This mechanism implements the [SDK-extensibility
redesign](https://github.com/initxy/noeta/blob/main/docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
(decision numbers `D1`–`D12` are cited inline).

## The model in one screen

- A **Plugin** (`D1`) is a package (or a single `.py` file) carrying a **static
  manifest**: a `name`, a `requires-noeta` range, an optional `config-schema`,
  and a list of **contributions**, each naming a **surface** plus a `ref` (an
  import string) or `path` (a resource).
- A **Surface** (`D2`/`D3`) is one extension point — `tool`, `guard`, `policy`,
  `reminder`, … Each has a `SurfaceSpec` describing how a contribution to it is
  validated, how it collides, how it merges, and how it orders. The loader is
  **surface-agnostic**: it consults the registry and nothing else, so a host can
  register its own surfaces.
- **Load** (`D5`, host level): `load_plugin_set(...) -> PluginSet` — which plugin
  *code* is available in the process. A `PluginSet` is listable and
  collision-checkable **without executing plugin code**.
- **Activate** (`D5`, agent level): `Options.plugins: list[str]` and
  `AgentDefinition.plugins` — which loaded plugins *this agent* uses. Activation
  enters `AgentSpec` identity. `Client(options, plugins=<PluginSet>)` binds the
  two together; an activation name that is not in the loaded set fails the build.

## Manifest format (`D1`)

A manifest is inert data — reading it imports **no** plugin code. There are two
forms.

### Distributed form — `[tool.noeta]` / `noeta-plugin.toml`

An installed package declares its manifest under `[tool.noeta]` in
`pyproject.toml` and **mirrors it into the wheel as package data**
`noeta-plugin.toml`, located via the distribution metadata. The reader
(`read_distribution_manifest`, `plugin_manifest.py`) reads that file straight off
disk for a regular install, and falls back to `importlib.util.find_spec` (which
locates a package without importing it) for an editable install — the
zero-execution guarantee holds in both.

```toml
# pyproject.toml — the plugin's manifest lives under [tool.noeta]
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
ref     = "house_style:HOUSE_STYLE"     # module:attr import string

[[tool.noeta.contributions]]
surface  = "tool"
ref      = "house_style.tools:LintTool"
```

`parse_manifest_text` accepts three TOML shapes, in priority order:
`[tool.noeta]` (a `pyproject.toml` that also carries the plugin), `[noeta]`, and
bare top-level keys (the mirrored `noeta-plugin.toml`).

### Manifest fields

| Field | Shape | Meaning |
| --- | --- | --- |
| `name` | `str`, required | the plugin's identity — the load-time dedup key and the activation name |
| `requires-noeta` | `str \| None` | a version range (advisory in v1) |
| `config-schema` | `table \| None` | an optional schema for operator config |
| `contributions` | array of tables | one entry per contribution |

Each contribution is a `ManifestContribution` (`plugin_manifest.py`):

| Key | Shape | Meaning |
| --- | --- | --- |
| `surface` | `str`, required | a registered surface name (see the [catalog](#surface-catalog-d3)) |
| `name` | `str` | collision / ordering key **and** listing label; derived from `ref` / `path` when omitted |
| `ref` | `str \| None` | a `module` or `module:qualname` import string — resolved **only** at the execution boundary |
| `path` | `str \| None` | a resource path (for resource-only surfaces such as `skills`) |
| `params` | remaining keys | surface-specific params kept verbatim (e.g. `priority` for `reminder`, `seams` for `reminder_provider`) |

When `name` is omitted it is derived from the `ref`'s attribute (or the module's
last segment), else from the `path` basename.

### Single-file form — `PluginBuilder`

A local `.py` plugin declares one module-level `PluginBuilder` and decorates its
contributions; the builder **is** the manifest (`D1`). Acceptable because local
files pass an explicit trust gate anyway.

```python
# brevity.py — a single-file plugin
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")

@plugin.reminder(priority=500)
def stay_brief(view):
    return None  # a real reminder returns str | None from the folded projection
```

`PluginBuilder(name, *, requires_noeta=None, config_schema=None)` exposes one
decorator/method per surface — each forwards to the generic `contribute(surface,
value, *, name=None, ref=None, path=None, **params)`:

| Method | Surface | Params |
| --- | --- | --- |
| `tool(fn=None, *, name=None)` | `tool` | — |
| `reminder(fn=None, *, name=None, priority=0)` | `reminder` | `priority` |
| `reminder_provider(fn=None, *, name=None, seams=())` | `reminder_provider` | `seams` |
| `tool_result_transform(fn=None, *, name=None, priority=0)` | `tool_result_transform` | `priority` |
| `guard(obj=None, *, name=None)` | `guard` | — |
| `observer(fn=None, *, name=None)` | `observer` | — |
| `prompt_fragment(text, *, name)` | `prompt_fragment` | — |
| `policy(factory=None, *, name=None)` | `policy` | — |
| `sandbox_provider(obj=None, *, name=None)` | `sandbox_provider` | — |

`manifest()` returns the equivalent `PluginManifest`; the decorated objects are
also cached (`resolved_objects`) so the loader resolves a single-file plugin's
contributions without a second import. `python -m noeta.sdk.plugin_check` (there
is **no** console script) derives and verifies the TOML from the decorators at
publish time.

## Surface catalog (`D3`)

The standard catalog is fourteen surfaces (`surfaces.py`, `STANDARD_SURFACES`).
Each row is a `SurfaceSpec`: which **plane** it lives on, how its effect is
**scoped** across agents (`D6`), its **collision key**, its **merge rule**, and
its **ordering**. ★ = new in this redesign.

| Surface | Plane | Scope (`D6`) | Collision key | Merge | Ordering | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `tool` | identity | per-agent | `name` | append | `(plugin, name)` | includes tool packs |
| `agent` | identity | per-agent | `name` | append | `(plugin, name)` | an `AgentDefinition` |
| `content_kind` | identity | per-agent | `kind` | append | `(plugin, name)` | a `ContentKindSpec` |
| `prompt_fragment` ★ | identity | per-agent | `name` | append | `(plugin, name)` | appended after the preset prompt |
| `policy` ★ | identity | per-agent | **single-valued** | single | — | base + active plugin, or two plugins, = error |
| `guard` | wiring | **process** | none | append | `(plugin, name)` | governance — see [scope](#effect-scoping-d6) |
| `observer` | wiring | **process** | none | append | `(plugin, name)` | governance — see [scope](#effect-scoping-d6) |
| `provider` | wiring | host-wired | **single-valued** | single | — | `Options.provider` collision = error |
| `reminder_provider` ★ | wiring | per-agent | `name` | append | `(plugin, name)` | recorded injection (track A) |
| `reminder` ★ | wiring | per-agent | `name` | append | **priority** | compose-time, pure (track B) |
| `tool_result_transform` ★ | wiring | per-agent | `name` | append | **priority** | ToolRuntime stage before recording |
| `mcp_server` | host | host-wired | `alias` | append | `(plugin, name)` | connectable server spec |
| `skills` | host | host-wired | none | append | `(plugin, name)` | resource-only (`path`) |
| `sandbox_provider` ★ | host | host-wired | `name` | append | `(plugin, name)` | host selects one |

- **Collision key** `none` means the surface never collides (guards / observers
  / skill dirs). `single-valued` means at most one across the whole loaded set.
- **Ordering** `priority` orders by an integer `priority` param first, ties
  broken by `(plugin, name)` — the guard-observer-hooks precedent. Everything
  else sorts by `(plugin, name)`, so discovery order never changes the result.

Host-defined surfaces extend this table (see [SurfaceRegistry](#surfacespec-surfaceregistry-d2)).

### Effect scoping (`D6`)

The one deliberate asymmetry — which surfaces follow per-agent activation and
which are process-wide:

| Surfaces | Rule |
| --- | --- |
| `tool` `agent` `content_kind` `prompt_fragment` `policy` `reminder_provider` `reminder` `tool_result_transform` | **follow per-agent activation** — feature semantics: an agent that does not activate the plugin does not get them |
| `guard` `observer` | **loaded ⇒ in force for every agent in the process.** Governance is operator authority; an agent author must not opt out of interception or audit by omitting an activation |
| `provider` `sandbox_provider` `mcp_server` `skills` | host wiring; the host selects and binds them, never per-agent |

## `SurfaceSpec` / `SurfaceRegistry` (`D2`)

The registry is the generality mechanism — the loader consults it and nothing
else, so **adding a surface is registering one `SurfaceSpec`, not editing the
loader**.

```python
@dataclass(frozen=True)
class SurfaceSpec:
    name: str
    plane: "identity" | "wiring" | "host"
    activation_scope: "per-agent" | "process" | "host-wired"
    validator: Callable[[Any], None]   # raises on an illegal contribution value
    collision_key: "name" | "kind" | "alias" | "single-valued" | "none"
    merge_rule: "append" | "single" | "dict-merge"
    ordering: "sorted" | "priority" = "sorted"
```

`validator` runs on a **resolved** value (after a `ref` is imported); listing and
manifest-level collision never call it, so they stay execution-free.

`standard_registry()` returns a fresh `SurfaceRegistry` seeded with the fourteen
standard surfaces. A host registers additional **app-plane** surfaces on a
**copy** before load — the same validation / collision / ordering pipeline runs
over them unchanged:

```python
from noeta.sdk import standard_registry, SurfaceSpec, PluginError

def _valid_route(value):
    if not callable(value):
        raise PluginError("http_route must be callable")

reg = standard_registry()                       # a fresh copy
reg.register(SurfaceSpec(
    "http_route", "host", "host-wired", _valid_route, "name", "append",
))
plugins = load_plugin_set(registry=reg, ...)    # the host's surface is live
```

`SurfaceRegistry` methods: `register(spec)` (a duplicate name raises),
`get(name)`, `names()`, `__contains__`, `copy()`.

## Sources and the load pipeline (`D4`)

Five sources, each with its own gate. Discovery order **never** affects the
result (only error attribution).

| # | Source | `load_plugin_set` argument | Gate |
| --- | --- | --- | --- |
| 0 | built-in plugins (`noeta.builtins`) | `builtins=True` (default) | on by default; disable per-name with `disabled_builtins` |
| 1 | entry points (`noeta.plugins` group) | `entry_points=True` | `enabled` allow-list, applied **before any import** |
| 2 | explicit modules / file paths | `modules=[...]` | caller-specified = authorized |
| 3 | `~/.noeta/plugins/` (or any) | `user_dirs=[...]` | the user's own machine = trusted |
| 4 | workspace `.noeta/plugins/` | `workspace_dirs=[...]` | trust store (untrusted dir → loud warning + skip) |

The pipeline for every candidate: **read manifest** (zero code execution for the
package / `.toml` forms) → **`enabled` gate before any import** → **trust gate**
(source 4 only) → **resolve `ref`s** → **validate per `SurfaceSpec`** →
**collision check** → **deterministic merge** sorted by `(plugin, contribution)`.
Resolution / validation only happen when a caller reaches the execution boundary
(`PluginSet.resolve` and friends); listing and merge run over the static
manifests alone.

### `load_plugin_set(...) -> PluginSet`

```python
load_plugin_set(
    *,
    builtins=True,               # bool | Iterable[PluginManifest]
    disabled_builtins=(),        # Iterable[str]
    entry_points=False,          # bool | Iterable[entry-point-like]
    modules=(),                  # Sequence[str] — dotted modules or file/dir/.toml paths
    user_dirs=(),                # Sequence[path] — scanned unconditionally
    workspace_dirs=(),           # Sequence[path] — scanned only when trusted
    enabled=None,                # Iterable[str] | None — allow-list of plugin names
    trust_store=None,            # Path | None — defaults to ~/.noeta/trust.json
    registry=None,               # SurfaceRegistry | None — defaults to standard_registry()
    entry_point_group="noeta.plugins",
) -> PluginSet
```

- `builtins=True` discovers the built-in catalog (`D11`); pass an iterable of
  `PluginManifest`s to inject a custom set (the testing seam). `disabled_builtins`
  drops built-ins by name, and the disable is **recorded** on the returned set
  (`PluginSet.disabled_builtins`) so a host can also honour it where no
  contribution expresses it — `skills` contributes nothing per-agent, so
  disabling it is what makes `Client` withhold the skills kit (no indexing, no
  `skill` tool, no skill content kind). Note that absence is not a disable:
  `builtins=False` scopes the *loaded set*, never the SDK's own capabilities.
- `react` **cannot** be disabled — `disabled_builtins=["react"]` raises
  `PluginError`. It supplies the default decision policy, whose identity every
  compiled `AgentSpec` pins as `POLICY_REF ("react", "1")`; an agent with no
  policy has no identity to compile and no parity to resume. The default brain
  is *replaceable*, not removable: activate a plugin contributing the `policy`
  surface and its ref takes over both the identity and the wired factory.
- `entry_points=True` discovers the `noeta.plugins` group via
  `importlib.metadata`; an iterable of entry-point-like objects (`.name` +
  `.dist`) injects them. An entry point whose distribution ships no
  `noeta-plugin.toml` fails loudly.
- `modules` entries may be a dotted module (importing it is authorized), a `.py`
  file, a directory (scanned like a source-3/4 dir), or a `.toml` manifest.
- `user_dirs` load unconditionally; `workspace_dirs` load only when the directory
  is recorded in the trust store, else are skipped with an
  `UntrustedPluginDirWarning`. Both scan sub-directories carrying a
  `noeta-plugin.toml` (zero execution) **and** top-level `*.py` single-file
  plugins (executed — a trusted directory), skipping files starting with `_`.

A cross-source duplicate plugin **name** is an error naming both origins.

## `PluginSet`

The loaded, host-level set (`plugin_set.py`). Frozen; holds the discovered
`LoadedPlugin`s plus the surface registry they loaded against.

| Member | Returns | Executes plugin code? |
| --- | --- | --- |
| `names()` / `__iter__` / `__len__` / `__contains__` / `get(name)` | listing | no |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)` — every contribution, optionally one surface | **no** (`D5` / acceptance-2) |
| `merged()` | `MergedContributions` — collision-checked, deterministically ordered | **no** |
| `resolve()` | `(ResolvedContribution, …)` — every contribution with its `ref` imported and validated | **yes** — the execution boundary |
| `identity_activations()` | `dict[str, PluginActivation]` — each **external** plugin's identity-plane contributions (tools / agents / prompt fragments / policy) | yes |
| `activation_transforms()` | `dict[str, ((priority, name, fn), …)]` — each external plugin's `tool_result_transform` stages | yes |
| `process_hooks()` | `(guards, observers)` — every **external** plugin's governance hooks, in `(plugin, name)` order | yes |

`contributions()` is the acceptance-2 guarantee: a caller sees exactly what an
installed plugin contributes without any of its code running.

```python
pset = load_plugin_set()                    # built-ins on
for plugin_name, contribution in pset.contributions("tool"):
    print(plugin_name, contribution.name)   # no plugin body imported

pset.get("memory").manifest.requires_noeta  # ">=0.4"
```

`Client` calls `identity_activations` / `activation_transforms` / `process_hooks`
during the build (never a mid-session turn); built-in plugins are excluded from
all three — their feature effect rides the capability-flag vocabulary compile
handles by name (see [Activation](#activation-d5-d6)), and the default guard /
observer stack is the engine's own.

## Activation (`D5` / `D6`)

Loading makes plugin code *available*; **activation** decides which loaded
plugins an agent uses. Activation names live on `Options.plugins` and
`AgentDefinition.plugins`, and enter `AgentSpec` identity.

```python
from noeta.sdk import Options, Client, load_plugin_set, DEFAULT_PLUGINS

pset = load_plugin_set(modules=["./brevity.py"])   # built-ins + the local plugin

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("memory", "brevity"),   # activate three by name
)

client = Client(options, provider=..., workspace_dir=".", plugins=pset)
```

An activation name must be one of:

- a **built-in feature bundle** that maps onto a `Capabilities` identity flag
  (`D5`): `memory`, `browser`, `skill_invocation`, `todo_write`,
  `ask_user_question`, `mcp` — activating one flips the matching flag and nothing
  else (`memory=True` becomes `plugins=["memory"]`);
- `delegation` — the one *structural* capability that is also authorable. It is
  normally derived (a root with `agents` delegates; a flat child does not), and
  activating it only ever turns it **on**: it is how a child agent is granted the
  right to spawn, which the retired `AgentDefinition.capabilities` used to do.
  `spawnable` stays derived from the `agents` dict — activation cannot name an
  agent;
- an **identity-inert built-in** recognised so a typo still fails loudly but with
  no compile effect: `fs`, `web`, `skills`, `reminders`, `governance`,
  `providers`, `presets`, `sandbox`;
- the **name of a loaded plugin** in the `PluginSet` handed to `Client` — its
  identity-plane contributions (extra tools / child agents / prompt fragments /
  policy) fold in.

`DEFAULT_PLUGINS = ("fs", "web")` is the default of `Options.plugins`; both are
identity-inert (the default 11-tool set still comes from `BUILTIN_TOOL_CLASSES`),
so a **bare `Options()` compiles byte-identically** to the pre-redesign spec — the
parity contract. `AgentDefinition.plugins` defaults to `()` (a child's tools come
from its own `tools` field).

`Capabilities` is retired as the activation vocabulary: `Capabilities(memory=True)`
becomes `plugins=["memory"]`, and the official presets declare their activation
sets this way (`presets/__init__.py`). The `Options.capabilities` /
`AgentDefinition.capabilities` authoring fields are **removed** — `plugins=` is the
only activation path. (The compiled `AgentSpec.capabilities` stays the identity
carrier; activation flips its flags.)

An unknown activation name fails compilation with a `ValueError` naming both the
offending name and where it appeared (`Options` or the child agent), listing the
built-in vocabulary and the loaded set — load it before activating, or fix the
name.

## Built-in plugins (`D11`)

noeta expresses its own capabilities as built-in plugins in `noeta/builtins/`
(the top-of-stack band beside `noeta.presets`). Since the 2026-07-29
microkernel migration each directory holds the manifest **and** the
implementation: `__init__.py` is the zero-execution `MANIFEST` (a
`PluginManifest` whose contributions carry `ref` strings), `impl/` is the code,
and the refs point at the sibling impl modules. Nothing in the manifest layer
imports the impl, so listing a built-in still runs zero capability code. The
loader reaches the catalog by a **dynamic** import (`builtin_manifests()`), and
`.importlinter`'s universal `sdk-core-not-builtins` contract keeps every band —
kernel included — free of static edges into `noeta.builtins`.

The thirteen built-ins (one directory per built-in under `noeta/builtins/` — the
canonical worked corpus of manifest declarations): `fs`, `web`, `memory`,
`browser`, `app`, `mcp`, `skills`, `react`, `reminders`, `governance`,
`providers`, `sandbox`, `presets`. Adding a first-party capability is adding a directory
here (plus a `SurfaceSpec` registration only when a genuinely new surface is
needed).

## Trust store

The workspace-directory trust store is a JSON file — `{"trusted": [abs path, …]}` —
at `DEFAULT_TRUST_STORE` (`~/.noeta/trust.json`) by default. Only `workspace_dirs`
consult it; `user_dirs` are always scanned.

| Function | Signature | Behaviour |
| --- | --- | --- |
| `is_trusted(path, store=None)` | `→ bool` | whether `path`'s canonical form is recorded; a missing store ⇒ `False`, never an error |
| `grant_trust(path, store=None)` | `→ None` | record `path`'s canonical form (idempotent); creates the store and its parent if absent |

Both sides canonicalise the path the same way — `~` expanded, absolute, symlinks
resolved — so how a path is spelled never decides trust. A malformed (non-JSON)
store raises `PluginError` on read.

```python
from noeta.sdk import grant_trust, load_plugin_set

grant_trust("./workspace/.noeta/plugins")                      # writes ~/.noeta/trust.json
pset = load_plugin_set(workspace_dirs=["./workspace/.noeta/plugins"])
```

## Failure semantics

Load faults are **loud** and fail the client build at startup — never a
mid-session turn:

- A bad or missing manifest, a broken file, an unimportable `ref`, a missing
  `ref` attribute, or a value that fails its surface `validator` raises
  `PluginError` naming the plugin.
- **Any collision** — two plugins claiming the same key; a cross-source duplicate
  plugin name; a second `policy` / `provider` — raises `PluginError` **naming both
  sides. There is no override.** (Collisions against a base `Options.policy` /
  `Options.provider` are caught by `compile_options` / the `Client` build.)
- An **unknown activation name** raises `ValueError` at compile.

The single non-raising skip is an **untrusted `workspace_dirs`** entry, which
warns with `UntrustedPluginDirWarning` and is skipped.

```python
from noeta.sdk import load_plugin_set, PluginError

pset = load_plugin_set(builtins=[m_a, m_b])   # both contribute prompt_fragment "frag"
try:
    pset.merged()
except PluginError as exc:
    #  prompt_fragment 'frag' on surface 'prompt_fragment' is contributed by both
    #  plugin 'a' and plugin 'b' — no override
    ...
```

## The retired bundle path

The 0.4.0 mechanism — `noeta_plugin(api)` factories, the `PluginAPI` accumulator,
`load_plugins` + `merge_plugins`, `LoadedPlugin`, `PluginContributions`, and
`merged_mcp_servers` / `merged_skill_dirs` — has been **removed** from `noeta.sdk`
and replaced outright by the manifest mechanism on this page (nothing of 0.4.0 was
published, so no compatibility was owed). Only the primitives the manifest
mechanism reuses survive in `client/plugins.py`: the trust-store functions
(`grant_trust` / `is_trusted`) and `PluginError` / `UntrustedPluginDirWarning`.

## See also

- [Write a plugin](../how-to/write-a-plugin.md) — the task-oriented guide
- [SDK reference](sdk.md) — `Options.plugins` activation and the `Client` /
  `query` `plugins=` argument
- [SDK-extensibility redesign](https://github.com/initxy/noeta/blob/main/docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
  — the full decision record (`D1`–`D12`)
- [ADR: Plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md)
  — the durable design rationale
