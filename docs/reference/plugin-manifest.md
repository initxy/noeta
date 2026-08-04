# Plugin manifest and loading

A manifest is inert data: a name, a version range, an optional config schema,
and a list of contributions. Reading one imports **no** plugin code, which is
what lets a host list and collision-check every installed plugin before
anything runs. This page covers the manifest's shape, the two forms it can take,
how `load_plugins` finds them, and how a plugin is packaged.

Source: `packages/noeta-sdk/noeta/client/{plugin_manifest,plugin_set,plugins}.py`.

## Distributed form: `[tool.noeta]`

An installed package declares its manifest under `[tool.noeta]` in
`pyproject.toml` and **mirrors it into the wheel as package data** named
`noeta-plugin.toml`.

```toml
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface  = "reminder"
ref      = "house_style.reminders:stay_brief"
priority = 500
```

`read_distribution_manifest` reads that file straight off disk for a regular
install and falls back to `importlib.util.find_spec` — which locates a package
without importing it — for an editable install. The zero-execution guarantee
holds either way.

`parse_manifest_text` accepts three TOML shapes, in priority order:
`[tool.noeta]` (a `pyproject.toml` that also carries the plugin), `[noeta]`, and
bare top-level keys (the mirrored `noeta-plugin.toml`).

### Manifest fields

| Field | Shape | Meaning |
| --- | --- | --- |
| `name` | `str`, required | the plugin's identity — the load-time dedup key **and** the activation name |
| `requires-noeta` | `str \| None` | a version range — checked at load (warn; refuse under `strict`) |
| `config-schema` | `table \| None` | an optional schema for operator config |
| `contributions` | array of tables | one entry per contribution |

### Contribution fields

Each entry becomes a `ManifestContribution`.

| Key | Shape | Meaning |
| --- | --- | --- |
| `surface` | `str`, required | a registered surface name — see [Plugin surfaces](plugin-surfaces.md) |
| `name` | `str` | the collision / ordering key **and** the listing label; derived from `ref` or `path` when omitted |
| `ref` | `str \| None` | a `module` or `module:qualname` import string, resolved **only** at the execution boundary |
| `path` | `str \| None` | a resource path, for resource-only surfaces such as `skills` |
| `params` | remaining keys | surface-specific and kept verbatim: `priority` for `reminder`, `seams` for `reminder_provider`, `text` for a literal `prompt_fragment` |

When `name` is omitted it is derived from the `ref`'s final attribute (or the
module's last segment), else from the `path` basename. `(surface, name)` must be
unique within one manifest; a duplicate raises `PluginError` naming both
entries.

## Single-file form: `PluginBuilder`

A local `.py` plugin declares one module-level `PluginBuilder` and decorates its
contributions. The builder **is** the manifest — acceptable because local files
pass an explicit trust gate anyway.

```python
# brevity.py — a single-file plugin
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")

@plugin.reminder(priority=500)
def stay_brief(view):
    return None   # a real reminder returns str | None from the folded view
```

`PluginBuilder(name, *, requires_noeta=None, config_schema=None)` exposes one
method per surface. Each forwards to the generic
`contribute(surface, value, *, name=None, ref=None, path=None, **params)`, which
also covers the surfaces with no dedicated method (`agent`, `content_kind`,
`mcp_server`, `skills`, `provider`).

| Method | Surface | Extra params |
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

`manifest()` returns the equivalent `PluginManifest`, and the decorated objects
are cached (`resolved_objects`) so the loader resolves a single-file plugin's
contributions without a second import.

## Version pinning: `requires-noeta`

`requires-noeta` records the SDK range a plugin was written against, and the
loader **evaluates it** against the installed `noeta-sdk` version at load:

| Outcome | Default | `load_plugins(strict=True)` |
| --- | --- | --- |
| range satisfied | silent | silent |
| range unsatisfied | `PluginVersionWarning` naming the plugin, its declared range and the installed version; **the plugin still loads** | `PluginError`, the load fails |
| specifier not understood | `PluginVersionWarning` "unrecognized requires-noeta specifier … not enforced" | same warning; **never** enforced |
| `noeta-sdk` has no installed metadata (an in-repo checkout) | treated as satisfied, logged at debug | same |

Warning-by-default because a range is the author's claim about what they tested,
not a lock: refusing on it would break a working deployment the first time the
SDK took a patch bump the plugin had not been re-tested against. `strict=True`
is for a deployment where "tested against this SDK" is a release gate.

The evaluator is deliberately minimal and dependency-free — `>=`, `>`, `<=`,
`<`, `==`, `!=` over dotted releases, AND-joined by commas, whitespace
tolerated. Anything richer (`~=`, extras, epochs, pre-release markers) reads as
unrecognised and is reported rather than guessed at, so a plugin is never
refused over a spelling the loader did not promise to understand.

```toml
requires-noeta = ">=0.6,<1.0"
```

Read it off a loaded plugin:

```python
print(pset.get("memory").manifest.requires_noeta)   # → '>=0.4'
```

## Sources and the load pipeline

Five sources, each with its own gate. Discovery order **never** affects the
result — only which origin an error names.

| # | Source | `load_plugins` argument | Gate |
| --- | --- | --- | --- |
| 0 | built-in plugins (`noeta.builtins`) | `builtins=True` (default) | on by default; disable per name with `disabled_builtins` |
| 1 | entry points (`noeta.plugins` group) | `entry_points=True` | the `enabled` allow-list, applied **before any import** |
| 2 | explicit modules or file paths | `modules=[...]` | caller-specified means authorized |
| 3 | `~/.noeta/plugins/` | `user_dirs=[...]` | the user's own machine, trusted |
| 4 | workspace `.noeta/plugins/` | `workspace_dirs=[...]` | the trust store; an untrusted dir warns and is skipped |

For every candidate: **read manifest** (zero code execution for the package and
`.toml` forms) → **`enabled` gate before any import** → **trust gate** (source 4
only) → **collision check** → **deterministic merge**, sorted by
`(plugin, contribution)`. Resolving `ref`s and running each surface's validator
happen only at the execution boundary (`PluginSet.resolve` and friends).

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
    strict=False,                # bool — refuse an unsatisfied requires-noeta
) -> PluginSet
```

- `builtins=True` discovers the built-in catalogue; pass an iterable of
  `PluginManifest`s to inject a custom set (the testing seam).
  `disabled_builtins` drops built-ins by name, and the disable is **recorded** on
  the returned set so a host can honour it where no contribution expresses it —
  disabling `skills` is what makes `Client` withhold the skills kit entirely.
  Absence is not a disable: `builtins=False` scopes the *loaded set*, never the
  SDK's own capabilities.
- `react` **cannot** be disabled — `disabled_builtins=["react"]` raises
  `PluginError`. It supplies the default decision policy that every compiled
  `AgentSpec` pins. The default brain is *replaceable* through the `policy`
  surface, not removable.
- `entry_points=True` discovers the `noeta.plugins` group via
  `importlib.metadata`; an iterable of entry-point-like objects (each exposing
  `.name` and `.dist`) injects them instead. An entry point whose distribution
  ships no `noeta-plugin.toml` fails loudly.
- `modules` entries may be a dotted module, a `.py` file, a directory (scanned
  like a source-3 or source-4 dir), or a `.toml` manifest.
- `user_dirs` load unconditionally; `workspace_dirs` load only when the directory
  is in the trust store. Both scan sub-directories carrying a
  `noeta-plugin.toml` (zero execution) **and** top-level `*.py` single-file
  plugins (executed — a trusted directory), skipping files starting with `_`.
- `strict=True` turns an unsatisfied `requires-noeta` from a warning into a
  `PluginError` (see the table above). An unparseable specifier still only
  warns.

A cross-source duplicate plugin **name** is an error naming both origins.

## `PluginSet`

The loaded, host-level set. Frozen; it holds the discovered plugins plus the
surface registry they loaded against. Every projection memoizes its resolution,
so one build imports each `ref` at most once.

| Member | Returns | Executes plugin code? |
| --- | --- | --- |
| `names()` / `__iter__` / `__len__` / `__contains__` / `get(name)` | listing | no |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)` | **no** |
| `merged()` | `MergedContributions` — collision-checked, deterministically ordered | **no** |
| `disabled_builtins` | `frozenset[str]` | no |
| `resolve()` | every contribution with its `ref` imported and validated | **yes** — the execution boundary |
| `identity_activations(only=None)` | each **external** plugin's identity-plane contributions | yes |
| `activation_transforms(only=None)` | `tool_result_transform` stages | yes |
| `activation_reminders(only=None)` | compose-time `reminder` renders | yes |
| `activation_reminder_providers(only=None)` | recorded `reminder_provider`s | yes |
| `activation_session_packs(only=None)` | `session_pack` factories | yes |
| `activation_control_tools(only=None)` | `control_tool` factories | yes |
| `process_hooks()` | `(guards, observers)` from external plugins, in `(plugin, name)` order | yes |
| `host_skills_dirs()` | external plugins' `skills` paths, in `(plugin, name)` order | yes |
| `host_mcp_servers()` | external plugins' `((alias, plugin, SdkMcpServer), …)` | yes |

`Client` calls the activation projections, `process_hooks` and the two
`host_*` projections during the build, never on a turn. The `host_*` pair and
`process_hooks` take **no** `only=`: their surfaces are process-wide or
host-wired, so loading is what puts them in force. Built-in plugins are
excluded from all of them — their effect
rides the activation vocabulary `compile_options` handles by name. The `only=`
argument restricts resolution to the names some agent actually activates, so a
loaded-but-unactivated plugin's module body never runs.

## Trust store

The workspace-directory trust store is a JSON file —
`{"trusted": [absolute path, …]}` — at `~/.noeta/trust.json` by default. Only
`workspace_dirs` consult it; `user_dirs` are always scanned.

| Function | Behaviour |
| --- | --- |
| `is_trusted(path, store=None) -> bool` | whether `path`'s canonical form is recorded; a missing store means `False`, never an error |
| `grant_trust(path, store=None) -> None` | record `path`'s canonical form (idempotent); creates the store and its parent if absent |

Both sides canonicalise the same way — `~` expanded, absolute, symlinks resolved
— so how a path is spelled never decides trust. A malformed store raises
`PluginError` on read.

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")     # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

## Failure semantics

Load faults are **loud** and fail the client build at startup, never a
mid-session turn.

- A bad or missing manifest, a broken file, an unimportable `ref`, a missing
  `ref` attribute, or a value that fails its surface validator raises
  `PluginError` naming the plugin.
- **Any collision** — two plugins claiming the same key, a cross-source
  duplicate plugin name, a second `policy` or `provider`, an `mcp_server` alias
  the recipe already uses — raises `PluginError` **naming both sides. There is
  no override.**
- A **`priority` that is present but not an integer** raises `PluginError`
  naming the plugin and the contribution. A priority-ordered surface orders on
  an int, so a coerced one would put the contribution at the front of a band its
  author meant to sit at the back of; an absent `priority` is still the
  documented default, `0`.
- An **unknown activation name** raises `ValueError` at compile time.

Three non-raising skips, each with a warning:

- an untrusted `workspace_dirs` entry — `UntrustedPluginDirWarning`;
- a **single-file plugin whose name cannot be read statically while an
  `enabled` allow-list is in force** — `UnnamedPluginFileWarning`. The allow-list
  authorizes *names*, so a file whose name can only be learned by running it
  offers no name to authorize, and executing it to find out would defeat the
  gate. Declare a module-level `noeta_plugin_name = "..."` (or a
  `PluginBuilder("...")` literal) to make the file gateable. With **no**
  allow-list the file is still executed and loaded as before.
- an unsatisfied or unparseable `requires-noeta` — `PluginVersionWarning` (see
  above; `strict=True` turns the unsatisfied case into a `PluginError`).

```python
from noeta.sdk import PluginError, load_plugins

pset = load_plugins(builtins=[m_a, m_b])   # both contribute prompt_fragment "frag"
try:
    pset.merged()
except PluginError as exc:
    print(exc)
# → prompt_fragment 'frag' on surface 'prompt_fragment' is contributed by both
#   plugin 'a' and plugin 'b' — no override
```

## Packaging

A distributed plugin ships two copies of the same data: `[tool.noeta]` in
`pyproject.toml`, and `noeta-plugin.toml` as package data inside the wheel. Keep
them in agreement — `python -m noeta.sdk.plugin_check` derives the TOML from a
`PluginBuilder` and verifies it at publish time. There is **no** console script.

Noeta's own built-ins follow the same layout, one directory per capability under
`packages/noeta-sdk/noeta/builtins/<name>/`: `__init__.py` holds the
zero-execution `MANIFEST`, `impl/` holds the code, and the manifest's `ref`s
point at the sibling impl modules. Nothing in the manifest layer imports the
impl, so listing a built-in runs zero capability code.

## Next

- [Plugin surfaces](plugin-surfaces.md) — what a contribution can be
- [Write a plugin](../how-to/write-a-plugin.md) — the task-oriented guide
- [Options](sdk-options.md) — the activation half of the contract
- [ADR: Plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md)
  — the durable design rationale
