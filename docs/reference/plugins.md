# Plugins reference

The plugin mechanism: **manifest-declared contribution packages over a surface
registry**, with a **host-level load / agent-level activation** split. A plugin
carries a static manifest listing its contributions; loading reads those
manifests (no plugin code runs) into a `PluginSet`; an agent then *activates*
the plugins it uses through `Options.plugins`. Source of truth:
`packages/noeta-sdk/noeta/client/{plugin_manifest,surfaces,plugin_set}.py`.

```python
from noeta.sdk import (
    # the manifest + the single-file builder
    PluginManifest, ManifestContribution, PluginBuilder,
    # the surface registry (the generality mechanism)
    SurfaceSpec, SurfaceRegistry, standard_registry,
    # the loader + the loaded set
    load_plugins, PluginSet,
    # activation
    PluginActivation, DEFAULT_PLUGINS,
    # trust + errors
    grant_trust, is_trusted, PluginError,
)
```

## The model in one screen

- A **Plugin** is a package (or a single `.py` file) carrying a **static
  manifest**: a `name`, a `requires-noeta` range, an optional `config-schema`,
  and a list of **contributions**, each naming a **surface** plus a `ref` (an
  import string) or `path` (a resource).
- A **Surface** is one extension point — `tool`, `guard`, `policy`, `reminder`,
  … Each has a `SurfaceSpec` describing how a contribution to it is validated,
  how it collides, and how it orders. The loader is **surface-agnostic**: it
  consults the registry and nothing else, so a host can register its own
  surfaces.
- **Load** (host level): `load_plugins(...) -> PluginSet` — which plugin *code*
  is available in the process. A `PluginSet` is listable and collision-checkable
  **without executing plugin code**.
- **Activate** (agent level): `Options.plugins: tuple[str, ...]` and
  `AgentDefinition.plugins` — which loaded plugins *this agent* uses. Activation
  enters `AgentSpec` identity. `Client(options, plugins=<PluginSet>)` binds the
  two together; an activation name that is not in the loaded set fails the
  build.

## Manifest format

A manifest is inert data — reading it imports **no** plugin code. There are two
forms.

### Distributed form — `[tool.noeta]` / `noeta-plugin.toml`

An installed package declares its manifest under `[tool.noeta]` in
`pyproject.toml` and **mirrors it into the wheel as package data**
`noeta-plugin.toml`, located via the distribution metadata.
`read_distribution_manifest` (`plugin_manifest.py`) reads that file straight off
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
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface  = "tool"
ref      = "house_style.tools:LintTool"     # module:attr import string
```

`parse_manifest_text` accepts three TOML shapes, in priority order:
`[tool.noeta]` (a `pyproject.toml` that also carries the plugin), `[noeta]`, and
bare top-level keys (the mirrored `noeta-plugin.toml`).

### Manifest fields

| Field | Shape | Meaning |
| --- | --- | --- |
| `name` | `str`, required | the plugin's identity — the load-time dedup key and the activation name |
| `requires-noeta` | `str \| None` | a version range (advisory) |
| `config-schema` | `table \| None` | an optional schema for operator config |
| `contributions` | array of tables | one entry per contribution |

Each contribution is a `ManifestContribution`:

| Key | Shape | Meaning |
| --- | --- | --- |
| `surface` | `str`, required | a registered surface name (see the [catalog](#surface-catalog)) |
| `name` | `str` | collision / ordering key **and** listing label; derived from `ref` / `path` when omitted |
| `ref` | `str \| None` | a `module` or `module:qualname` import string — resolved **only** at the execution boundary |
| `path` | `str \| None` | a resource path (for resource-only surfaces such as `skills`) |
| `params` | remaining keys | surface-specific params kept verbatim (`priority` for `reminder`, `seams` for `reminder_provider`, `text` for a literal-valued `prompt_fragment`) |

When `name` is omitted it is derived from the `ref`'s final attribute (or the
module's last segment), else from the `path` basename. `(surface, name)` must be
unique within one manifest; a duplicate raises `PluginError` naming both
entries.

### Single-file form — `PluginBuilder`

A local `.py` plugin declares one module-level `PluginBuilder` and decorates its
contributions; the builder **is** the manifest. Acceptable because local files
pass an explicit trust gate anyway.

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
value, *, name=None, ref=None, path=None, **params)`, which also covers surfaces
with no dedicated method (`agent`, `content_kind`, `mcp_server`, `skills`,
`provider`):

| Method | Surface | Params |
| --- | --- | --- |
| `tool(fn=None, *, name=None)` | `tool` | — |
| `reminder(fn=None, *, name=None, priority=0)` | `reminder` | `priority` |
| `reminder_provider(fn=None, *, name=None, seams=())` | `reminder_provider` | `seams` |
| `tool_result_transform(fn=None, *, name=None, priority=0)` | `tool_result_transform` | `priority` |
| `guard(obj=None, *, name=None)` | `guard` | — |
| `observer(fn=None, *, name=None)` | `observer` | — |
| `prompt_fragment(text, *, name)` | `prompt_fragment` | `text` |
| `policy(factory=None, *, name=None)` | `policy` | — |
| `sandbox_provider(obj=None, *, name=None)` | `sandbox_provider` | — |
| `session_pack(factory=None, *, name=None, priority=0)` | `session_pack` | `priority` |
| `control_tool(factory=None, *, name=None, priority=0)` | `control_tool` | `priority` |

`manifest()` returns the equivalent `PluginManifest`; the decorated objects are
also cached (`resolved_objects`) so the loader resolves a single-file plugin's
contributions without a second import. `python -m noeta.sdk.plugin_check` (there
is **no** console script) derives and verifies the TOML from the decorators at
publish time.

## Surface catalog

The standard catalog is sixteen surfaces
(`noeta.client.surfaces.STANDARD_SURFACES`). Each row is a `SurfaceSpec`: which
**plane** it lives on, how its effect is **scoped** across agents, its
**collision key**, and its **ordering**.

| Surface | Plane | Scope | Collision key | Ordering | Notes |
| --- | --- | --- | --- | --- | --- |
| `tool` | identity | per-agent | `name` | `(plugin, name)` | a built-in tool name or a `.ref`-bearing tool |
| `agent` | identity | per-agent | `name` | `(plugin, name)` | an `AgentDefinition` |
| `content_kind` | identity | per-agent | `kind` | `(plugin, name)` | a `ContentKindSpec` |
| `prompt_fragment` | identity | per-agent | `name` | `(plugin, name)` | a literal string, appended after the prompt |
| `policy` | identity | per-agent | **single-valued** | — | base + active plugin, or two plugins, = error |
| `guard` | wiring | **process** | none | `(plugin, name)` | governance — see [scope](#effect-scoping) |
| `observer` | wiring | **process** | none | `(plugin, name)` | governance — see [scope](#effect-scoping) |
| `provider` | wiring | host-wired | **single-valued** | — | `Options.provider` collision = error |
| `reminder_provider` | wiring | per-agent | `name` | `(plugin, name)` | recorded injection at a named seam |
| `reminder` | wiring | per-agent | `name` | **priority** | compose-time, pure |
| `tool_result_transform` | wiring | per-agent | `name` | **priority** | ToolRuntime stage before recording |
| `mcp_server` | host | host-wired | `alias` | `(plugin, name)` | connectable server spec |
| `skills` | host | host-wired | none | `(plugin, name)` | resource-only (`path`) |
| `sandbox_provider` | host | host-wired | `name` | `(plugin, name)` | host selects one |
| `session_pack` | wiring | per-agent | `name` | **priority** | session-construction factory `(SessionBuildContext) -> PackContribution` |
| `control_tool` | identity | per-agent | `name` | **priority** | control-tool factory `(ControlToolBuildContext) -> ControlToolMount \| None` |

- **Collision key** `none` means the surface never collides (guards / observers
  / skill dirs). `single-valued` means at most one across the whole loaded set.
- **Ordering** `priority` orders by an integer `priority` param first, ties
  broken by `(plugin, name)`. Everything else sorts by `(plugin, name)`, so
  discovery order never changes the result.

Host-defined surfaces extend this table (see
[SurfaceRegistry](#surfacespec--surfaceregistry)).

### Effect scoping

The one deliberate asymmetry — which surfaces follow per-agent activation and
which are process-wide:

| Surfaces | Rule |
| --- | --- |
| `tool` `agent` `content_kind` `prompt_fragment` `policy` `reminder_provider` `reminder` `tool_result_transform` `session_pack` `control_tool` | **follow per-agent activation** — feature semantics: an agent that does not activate the plugin does not get them |
| `guard` `observer` | **loaded ⇒ in force for every agent in the process.** Governance is operator authority; an agent author must not opt out of interception or audit by omitting an activation |
| `provider` `sandbox_provider` `mcp_server` `skills` | host wiring; the host selects and binds them, never per-agent |

## `SurfaceSpec` / `SurfaceRegistry`

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
    ordering: "sorted" | "priority" = "sorted"
    # identity-plane only — which PluginActivation channel the contribution
    # feeds; "elsewhere" when a per-agent projection carries it instead
    activation_binding: "tool" | "agent" | "content_kind"
                      | "prompt_fragment" | "policy" | "elsewhere" | None = None
```

`validator` runs on a **resolved** value (after a `ref` is imported); listing and
manifest-level collision never call it, so they stay execution-free. Every enum
field is checked at construction, so a mistyped value (or a positional argument
in the wrong slot) raises `PluginError` at the registration line.

`activation_binding` keeps the identity projection **table-driven**: an
identity-plane surface declares the channel it feeds and reaches
`compile_options` with no loader edit. It is **required** for `plane="identity"`
(an identity contribution with no binding would be silently dropped between
resolve and compile) and **rejected** for every other plane.

The wiring plane has exactly two **process-wide** channels, `guard` and
`observer`. `PluginSet.process_hooks()` refuses a third rather than filing it
under one of them: handing the engine a non-`Guard` value would turn a
build-time configuration error into a crash on the first tool call. Give such a
surface a per-agent scope instead.

`standard_registry()` returns a fresh `SurfaceRegistry` seeded with the sixteen
standard surfaces. A host registers additional surfaces on a **copy** before
load — the same validation / collision / ordering pipeline runs over them
unchanged:

```python
from noeta.sdk import standard_registry, SurfaceSpec, PluginError, load_plugins

def _valid_route(value):
    if not callable(value):
        raise PluginError("http_route must be callable")

reg = standard_registry()                       # a fresh copy
reg.register(SurfaceSpec(
    "http_route", "host", "host-wired", _valid_route, "name",
))
plugins = load_plugins(registry=reg)            # the host's surface is live
```

`SurfaceRegistry` methods: `register(spec)` (a duplicate name raises),
`get(name)`, `names()`, `__contains__`, `copy()`.

## Sources and the load pipeline

Five sources, each with its own gate. Discovery order **never** affects the
result (only error attribution).

| # | Source | `load_plugins` argument | Gate |
| --- | --- | --- | --- |
| 0 | built-in plugins (`noeta.builtins`) | `builtins=True` (default) | on by default; disable per-name with `disabled_builtins` |
| 1 | entry points (`noeta.plugins` group) | `entry_points=True` | `enabled` allow-list, applied **before any import** |
| 2 | explicit modules / file paths | `modules=[...]` | caller-specified = authorized |
| 3 | `~/.noeta/plugins/` (or any) | `user_dirs=[...]` | the user's own machine = trusted |
| 4 | workspace `.noeta/plugins/` | `workspace_dirs=[...]` | trust store (untrusted dir → loud warning + skip) |

The pipeline for every candidate: **read manifest** (zero code execution for the
package / `.toml` forms) → **`enabled` gate before any import** → **trust gate**
(source 4 only) → **collision check** → **deterministic merge** sorted by
`(plugin, contribution)`. Resolving `ref`s and running each surface's
`validator` happen only when a caller reaches the execution boundary
(`PluginSet.resolve` and friends); listing and merge run over the static
manifests alone.

### `load_plugins(...) -> PluginSet`

```python
load_plugins(
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

- `builtins=True` discovers the built-in catalog; pass an iterable of
  `PluginManifest`s to inject a custom set (the testing seam).
  `disabled_builtins` drops built-ins by name, and the disable is **recorded**
  on the returned set (`PluginSet.disabled_builtins`) so a host can also honour
  it where no contribution expresses it — disabling `skills` is what makes
  `Client` withhold the skills kit (no indexing, no `skill` control tool, no
  skill content kind). Absence is not a disable: `builtins=False` scopes the
  *loaded set*, never the SDK's own capabilities.
- `react` **cannot** be disabled — `disabled_builtins=["react"]` raises
  `PluginError`. It supplies the default decision policy, whose identity every
  compiled `AgentSpec` pins as `POLICY_REF ("react", "1")`. The default brain is
  *replaceable*, not removable: activate a plugin contributing the `policy`
  surface and its ref takes over both the identity and the wired factory.
- `entry_points=True` discovers the `noeta.plugins` group via
  `importlib.metadata`; an iterable of entry-point-like objects (`.name` +
  `.dist`) injects them. An entry point whose distribution ships no
  `noeta-plugin.toml` fails loudly.
- `modules` entries may be a dotted module (importing it is authorized), a `.py`
  file, a directory (scanned like a source-3/4 dir), or a `.toml` manifest.
- `user_dirs` load unconditionally; `workspace_dirs` load only when the
  directory is recorded in the trust store, else are skipped with an
  `UntrustedPluginDirWarning`. Both scan sub-directories carrying a
  `noeta-plugin.toml` (zero execution) **and** top-level `*.py` single-file
  plugins (executed — a trusted directory), skipping files starting with `_`.

A cross-source duplicate plugin **name** is an error naming both origins.

## `PluginSet`

The loaded, host-level set (`plugin_set.py`). Frozen; holds the discovered
`LoadedPlugin`s plus the surface registry they loaded against. Every projection
below memoizes its resolution, so one build imports each `ref` at most once.

| Member | Returns | Executes plugin code? |
| --- | --- | --- |
| `names()` / `__iter__` / `__len__` / `__contains__` / `get(name)` | listing | no |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)` — every contribution, optionally one surface | **no** |
| `merged()` | `MergedContributions` — collision-checked, deterministically ordered | **no** |
| `disabled_builtins` | `frozenset[str]` — the built-ins the caller turned off | no |
| `resolve()` | `(ResolvedContribution, …)` — every contribution with its `ref` imported and validated | **yes** — the execution boundary |
| `identity_activations(only=None)` | `dict[str, PluginActivation]` — each **external** plugin's identity-plane contributions (tools / agents / content kinds / prompt fragments / policy) | yes |
| `activation_transforms(only=None)` | `dict[str, ((priority, name, fn), …)]` — `tool_result_transform` stages | yes |
| `activation_reminders(only=None)` | `dict[str, ((priority, name, render), …)]` — compose-time `reminder` renders | yes |
| `activation_reminder_providers(only=None)` | `dict[str, ((seams, name, provider), …)]` — recorded `reminder_provider`s | yes |
| `activation_session_packs(only=None)` | `dict[str, ((priority, name, factory), …)]` — `session_pack` factories | yes |
| `activation_control_tools(only=None)` | `dict[str, ((priority, name, factory), …)]` — `control_tool` factories | yes |
| `process_hooks()` | `(guards, observers)` — every **external** plugin's governance hooks, in `(plugin, name)` order | yes |

`contributions()` is the listing guarantee: a caller sees exactly what an
installed plugin contributes without any of its code running.

```python
pset = load_plugins()                    # built-ins on
for plugin_name, contribution in pset.contributions("tool"):
    print(plugin_name, contribution.name)   # no plugin body imported

pset.get("memory").manifest.requires_noeta  # ">=0.4"
```

`Client` calls the activation projections and `process_hooks` during the build,
never a mid-session turn. Built-in plugins are excluded from all of them — their
feature effect rides the activation vocabulary `compile_options` handles by
name, and the default guard / observer stack is the engine's own. The `only=`
argument restricts resolution to the names some agent actually activates, so a
loaded-but-unactivated plugin's module body never runs.

## Activation

Loading makes plugin code *available*; **activation** decides which loaded
plugins an agent uses. Activation names live on `Options.plugins` and
`AgentDefinition.plugins`, and enter `AgentSpec` identity.

```python
from noeta.sdk import Options, Client, load_plugins, DEFAULT_PLUGINS

pset = load_plugins(modules=["./brevity.py"])   # built-ins + the local plugin

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("memory", "brevity"),
)

client = Client(options, provider=..., workspace_dir=".", plugins=pset)
```

An activation name must be one of:

- a **built-in feature bundle** that folds into the `AgentSpec.plugins`
  activation tuple: `memory`, `browser`, `mcp`, `todo_write`,
  `ask_user_question`, `skill_invocation`, `delegation`. Activating one adds its
  name to the tuple and nothing else; `agent_activates(agent, plugin)` is the
  membership read. `todo_write` / `ask_user_question` / `delegation` are also
  real built-in plugins contributing on the `control_tool` surface;
  `skill_invocation` is a recognised non-plugin activation (it gates the `skill`
  control tool inside the `skills` built-in). `delegation` is additionally
  *derived* — a root with `agents` delegates, a flat child does not — so naming
  it only ever turns it **on**, granting a child the right to spawn. `spawnable`
  stays derived from the `agents` dict; activation cannot name an agent;
- an **identity-inert built-in** recognised so a typo still fails loudly but
  with no compile effect: `app`, `fs`, `governance`, `presets`, `providers`,
  `react`, `reminders`, `sandbox`, `skills`, `storage`, `web`, `workspace`;
- the **name of a loaded plugin** in the `PluginSet` handed to `Client` — its
  identity-plane contributions (extra tools / child agents / content kinds /
  prompt fragments / policy) fold in.

`DEFAULT_PLUGINS = ("fs", "web")` is the default of `Options.plugins`; both are
identity-inert (the default 11-tool set comes from `builtin_tool_classes()`,
which reads the `fs` and `web` manifests), so a bare `Options()` compiles to the
same `AgentSpec` with or without them. `AgentDefinition.plugins` defaults to
`()` — a child's tools come from its own `tools` field.

An unknown activation name fails compilation with a `ValueError` naming both the
offending name and where it appeared (`Options` or the child agent), listing the
built-in vocabulary and the loaded set — load it before activating, or fix the
name.

## Built-in plugins

noeta expresses its own capabilities as built-in plugins in
`packages/noeta-sdk/noeta/builtins/` — the top-of-stack band beside
`noeta.presets`. Each directory holds the manifest **and** the implementation:
`__init__.py` is the zero-execution `MANIFEST` (a `PluginManifest` whose
contributions carry `ref` strings), `impl/` is the code, and the refs point at
the sibling impl modules. Nothing in the manifest layer imports the impl, so
listing a built-in runs zero capability code. The loader reaches the catalog by
a **dynamic** import (`builtin_manifests()`), and `.importlinter`'s
`sdk-core-not-builtins` contract keeps every band — kernel included — free of
static edges into `noeta.builtins`.

The eighteen built-ins, one directory each, are the canonical worked corpus of
manifest declarations:

| Built-in | Contributes |
| --- | --- |
| `fs` | nine `tool`s (`read`, `glob`, `grep`, `edit`, `write`, `apply_patch`, `shell_run`, `shell_poll`, `shell_kill`) + a `session_pack` |
| `web` | `webfetch` / `web_search` `tool`s + a `session_pack` |
| `memory` | four memory `tool`s, a `prompt_fragment`, a `reminder_provider`, a `session_pack` |
| `browser` | a `session_pack` (sandbox-backed browser tools) |
| `app` | a `session_pack` (the gateway-gated `open_app` tool) |
| `skills` | a `session_pack` |
| `react` | the `run_workflow` / `structured_output` `control_tool`s |
| `reminders` | three `reminder`s |
| `governance` | four `guard`s + one `observer` |
| `sandbox` | two `sandbox_provider`s |
| `presets` | the `web` and `__consolidation__` `agent`s |
| `workspace` | two `session_pack`s (instructions, environment) |
| `todo_write` | one `control_tool` |
| `ask_user_question` | one `control_tool` |
| `delegation` | the `spawn_subagent` `control_tool` |
| `mcp`, `providers`, `storage` | declaration-only (zero contributions); their code is reached through the SDK's own accessors |

Adding a first-party capability is adding a directory here, plus a `SurfaceSpec`
registration only when a genuinely new surface is needed.

## Trust store

The workspace-directory trust store is a JSON file — `{"trusted": [abs path, …]}` —
at `DEFAULT_TRUST_STORE` (`~/.noeta/trust.json`) by default. Only
`workspace_dirs` consult it; `user_dirs` are always scanned.

| Function | Signature | Behaviour |
| --- | --- | --- |
| `is_trusted(path, store=None)` | `→ bool` | whether `path`'s canonical form is recorded; a missing store ⇒ `False`, never an error |
| `grant_trust(path, store=None)` | `→ None` | record `path`'s canonical form (idempotent); creates the store and its parent if absent |

Both sides canonicalise the path the same way — `~` expanded, absolute, symlinks
resolved — so how a path is spelled never decides trust. A malformed (non-JSON)
store raises `PluginError` on read.

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")                      # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

## Failure semantics

Load faults are **loud** and fail the client build at startup — never a
mid-session turn:

- A bad or missing manifest, a broken file, an unimportable `ref`, a missing
  `ref` attribute, or a value that fails its surface `validator` raises
  `PluginError` naming the plugin.
- **Any collision** — two plugins claiming the same key; a cross-source
  duplicate plugin name; a second `policy` / `provider` — raises `PluginError`
  **naming both sides. There is no override.** (Collisions against a base
  `Options.policy` / `Options.provider` are caught by `compile_options` / the
  `Client` build.)
- An **unknown activation name** raises `ValueError` at compile.

The single non-raising skip is an **untrusted `workspace_dirs`** entry, which
warns with `UntrustedPluginDirWarning` and is skipped.

```python
from noeta.sdk import load_plugins, PluginError

pset = load_plugins(builtins=[m_a, m_b])   # both contribute prompt_fragment "frag"
try:
    pset.merged()
except PluginError as exc:
    #  prompt_fragment 'frag' on surface 'prompt_fragment' is contributed by both
    #  plugin 'a' and plugin 'b' — no override
    ...
```

## See also

- [Write a plugin](../how-to/write-a-plugin.md) — the task-oriented guide
- [SDK reference](sdk.md) — `Options.plugins` activation and the `Client` /
  `query` `plugins=` argument
- [ADR: Plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md)
  — the durable design rationale
