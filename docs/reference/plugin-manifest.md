# Plugin manifest and loading

A manifest is inert data — name, version range, optional config schema, contributions — so a host can list and collision-check every plugin without importing any plugin code.

Source: `packages/noeta-sdk/noeta/client/{plugin_manifest,plugin_set,plugins}.py`.

## Package form: `[tool.noeta]`

Declare the manifest in `pyproject.toml` and ship the same data as package data named `noeta-plugin.toml`.

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

`parse_manifest_text` accepts `[tool.noeta]`, `[noeta]`, or bare top-level keys, in that order. `read_distribution_manifest` reads the file off disk, or locates an editable install with `importlib.util.find_spec` — neither imports the package.

### Manifest fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | required | plugin identity: dedup key and activation name |
| `requires-noeta` | `str` | `None` | SDK version range, checked at load |
| `config-schema` | table | `None` | schema for operator config |
| `contributions` | array of tables | empty | one entry per contribution |

### Contribution fields

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `surface` | `str` | required | a registered [surface](plugin-surfaces.md) |
| `name` | `str` | derived | collision / ordering key; derived from `ref`'s last attribute (or module segment), else `path`'s basename |
| `ref` | `str` | `None` | `module` or `module:qualname`, imported only when resolved |
| `path` | `str` | `None` | resource path for resource-only surfaces (`skills`) |
| other keys | any | — | surface params kept verbatim: `priority`, `seams`, `text` |

`(surface, name)` must be unique within one manifest, else `PluginError`.

## Single-file form: `PluginBuilder`

A local `.py` plugin declares one module-level `PluginBuilder`; the builder is the manifest.

```python
# brevity.py
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")

@plugin.reminder(priority=500)
def stay_brief(view):
    return None   # return str | None from the folded view
```

`PluginBuilder(name, *, requires_noeta=None, config_schema=None)`. Every method forwards to `contribute(surface, value=None, *, name=None, ref=None, path=None, **params)`, which also covers `agent`, `content_kind`, `mcp_server`, `skills` and `provider`.

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

`manifest()` returns the equivalent `PluginManifest`; decorated objects are cached in `resolved_objects` so the file is not imported twice.

## `requires-noeta`

Evaluated against the installed `noeta-sdk` at load.

| Outcome | Default | `strict=True` |
| --- | --- | --- |
| satisfied | silent | silent |
| unsatisfied | `PluginVersionWarning`; plugin still loads | `PluginError` |
| specifier not understood | `PluginVersionWarning`, not enforced | same |
| `noeta-sdk` has no metadata (repo checkout) | treated as satisfied | same |

Supported: `>=`, `>`, `<=`, `<`, `==`, `!=` over dotted releases, comma-joined (`">=0.6,<1.0"`). `~=`, extras, epochs and pre-release markers read as unrecognised.

## `load_plugins`

```python
load_plugins(
    *,
    builtins=True,               # bool | Iterable[PluginManifest]
    disabled_builtins=(),        # Iterable[str]
    entry_points=False,          # bool | Iterable[entry-point-like]
    modules=(),                  # dotted modules, .py files, dirs, or .toml paths
    user_dirs=(),                # always scanned
    workspace_dirs=(),           # scanned only when trusted
    enabled=None,                # allow-list of plugin names, applied before any import
    trust_store=None,            # default ~/.noeta/trust.json
    registry=None,               # default standard_registry()
    entry_point_group="noeta.plugins",
    strict=False,                # refuse an unsatisfied requires-noeta
) -> PluginSet
```

| # | Source | Argument | Gate |
| --- | --- | --- | --- |
| 0 | built-ins (`noeta.builtins`) | `builtins=True` | on; drop by name with `disabled_builtins` |
| 1 | entry points (`noeta.plugins`) | `entry_points=True` | `enabled` allow-list |
| 2 | explicit modules / paths | `modules=[...]` | caller-specified |
| 3 | `~/.noeta/plugins/` | `user_dirs=[...]` | trusted |
| 4 | workspace `.noeta/plugins/` | `workspace_dirs=[...]` | trust store; untrusted dirs warn and are skipped |

Pipeline per candidate: read manifest → `enabled` gate → trust gate (source 4) → collision check → merge sorted by `(plugin, contribution)`. Discovery order never changes the result. `ref`s are imported and validated only at resolution.

- `disabled_builtins` is recorded on the set; disabling `skills` makes `Client` drop the skills kit. `disabled_builtins=["react"]` raises — `react` supplies the default policy, which is replaceable via the `policy` surface, not removable.
- An entry point whose distribution ships no `noeta-plugin.toml` fails.
- Directory scans load sub-directories with a `noeta-plugin.toml` (no execution) and top-level `*.py` files (executed), skipping names starting with `_`.
- A duplicate plugin name across sources is an error naming both origins.

## `PluginSet`

Frozen; each projection memoizes, so each `ref` is imported at most once.

| Member | Returns | Runs plugin code |
| --- | --- | --- |
| `names()`, `__iter__`, `__len__`, `__contains__`, `get(name)` | listing | no |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)` | no |
| `merged()` | `MergedContributions`, collision-checked and ordered | no |
| `disabled_builtins` | `frozenset[str]` | no |
| `resolve()` | every contribution imported and validated | yes |
| `identity_activations(only=None)` | external plugins' identity contributions | yes |
| `activation_transforms(only=None)` | `tool_result_transform` stages | yes |
| `activation_reminders(only=None)` | `reminder` renders | yes |
| `activation_reminder_providers(only=None)` | `reminder_provider`s | yes |
| `activation_session_packs(only=None)` | `session_pack` factories | yes |
| `activation_control_tools(only=None)` | `control_tool` factories | yes |
| `process_hooks()` | `(guards, observers)` from external plugins | yes |
| `host_skills_dirs()` | external `skills` paths | yes |
| `host_mcp_servers()` | `((alias, plugin, SdkMcpServer), …)` | yes |

`Client` calls these once at build. `only=` limits resolution to plugins some agent activates. Built-ins are excluded from all projections; they act through activation names.

## Trust store

A JSON file, `{"trusted": [absolute path, …]}`, at `~/.noeta/trust.json` by default. Only `workspace_dirs` (and workspace skill / shell-allowlist files) consult it.

| Function | Behaviour |
| --- | --- |
| `is_trusted(path, store=None) -> bool` | whether the canonical path is recorded; missing store → `False` |
| `grant_trust(path, store=None) -> None` | record the canonical path (idempotent); creates the store |

Paths are canonicalised (`~` expanded, absolute, symlinks resolved). A malformed store raises `PluginError`.

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

## Failures

Load faults fail the client build at startup, never mid-turn.

| Condition | Result |
| --- | --- |
| bad or missing manifest, unimportable `ref`, value fails its surface validator | `PluginError` naming the plugin |
| any collision (same key, duplicate plugin name, second `policy` / `provider`, `mcp_server` alias clash) | `PluginError` naming both sides; no override |
| `priority` present but not an int | `PluginError` |
| unknown activation name | `ValueError` at compile |
| untrusted `workspace_dirs` entry | skipped, `UntrustedPluginDirWarning` |
| single-file plugin whose name can't be read statically while `enabled` is set | skipped, `UnnamedPluginFileWarning`; declare `noeta_plugin_name = "..."` or a literal `PluginBuilder("...")` |
| unsatisfied / unparseable `requires-noeta` | `PluginVersionWarning` |

## Packaging

Keep `[tool.noeta]` and the wheel's `noeta-plugin.toml` in agreement; `python -m noeta.sdk.plugin_check` derives the TOML from a `PluginBuilder` and verifies it. Built-ins use the same layout: `noeta/builtins/<name>/__init__.py` holds the `MANIFEST`, `impl/` holds the code.

## Next

- [Plugin surfaces](plugin-surfaces.md) — what a contribution can be
- [Write a plugin](../guides/plugins.md) — the task guide
- [Plugin system](../how-it-works/plugin-system.md) — how surfaces and loading fit together
