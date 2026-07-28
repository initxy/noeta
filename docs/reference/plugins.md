# Plugins reference (`noeta.client.plugins`)

The plugin mechanism — discoverable bundles of typed `Options` contributions,
folded deterministically into a base `Options` before `compile_options`. Every
symbol below is re-exported through `noeta.sdk`; source of truth:
`packages/noeta-sdk/noeta/client/plugins.py`.

```python
from noeta.sdk import (
    PluginAPI, load_plugins, merge_plugins,
    merged_mcp_servers, merged_skill_dirs,
    grant_trust, is_trusted,
    PluginError, LoadedPlugin, PluginContributions, UntrustedPluginDirWarning,
)
```

A **Plugin** is a Python module exporting a `noeta_plugin(api)` factory. The
factory records contributions onto a `PluginAPI`; loading runs the factory;
`merge_plugins` folds the results into an `Options`. The mechanism adds no engine
power — it only populates the open extension surfaces `Options` already exposes.

> Line numbers are omitted throughout — they drift on every edit. The module path
> plus the member name is the stable coordinate.

## Two planes

A plugin's contributions split by where they land:

- **Identity plane** — tools, agents, content kinds, provider, guards,
  observers — fold into the `Options` returned by `merge_plugins`, and so into
  `AgentSpec` identity (tools + agents) or its wiring (the rest).
- **Host plane** — MCP server specs and skill directories — are validated and
  collision-checked but do **not** enter `Options` (they have no `Options`
  surface). A host reads them with `merged_mcp_servers` / `merged_skill_dirs`
  and wires them into its `HostConfig`.

## `PluginAPI`

The accumulator a factory receives (`plugins.py`). A pure recorder — no live
engine handles; every method appends a typed contribution and validates eagerly
where the check is cheap. A bad entry, a second provider, or a duplicate name
*within one plugin* raises `PluginError` at factory time, naming the plugin.

| Method | Records | Collision key | Eager validation (raises `PluginError`) |
| --- | --- | --- | --- |
| `add_tool(tool)` | a built-in name string or a `.ref`-bearing tool | the resolved tool name | unknown built-in name / bad `.ref`; duplicate name in this plugin |
| `add_guard(guard)` | a `Guard` | — | `None` guard |
| `add_observer(observer)` | a post-commit `Observer` | — | non-callable observer |
| `set_provider(provider)` | the single `LLMProvider` | (single-valued) | `None` provider; a second call |
| `add_content_kind(spec)` | a `ContentKindSpec` | `spec.kind` | non-`ContentKindSpec`; duplicate kind |
| `add_agent(name, definition)` | an `AgentDefinition` child | `name` | empty name; non-`AgentDefinition`; duplicate name |
| `add_mcp_server(alias, spec)` | a host-plane MCP server spec | `alias` | empty alias; `None` spec; duplicate alias |
| `add_skill_dir(path)` | a host-plane skill directory (coerced to an absolute `Path`) | — | empty path. Existence is **not** required — the directory may be provisioned later |

`add_tool` resolves each entry to its `ToolRef` immediately, so the name it
collides on is fixed at factory time. `add_skill_dir` stores an absolute path
but does not stat it.

## `load_plugins(...)`

Discover and invoke plugins from up to three opt-in sources. Every source is off
unless its argument is supplied; a bare `load_plugins()` returns `[]`.

```python
load_plugins(
    *,
    entry_points=False,
    modules=(),
    trusted_dirs=(),
    workspace_dirs=(),
    enabled=None,
    config=None,
    trust_store=None,
    entry_point_group="noeta.plugins",
) -> list[LoadedPlugin]
```

| Parameter | Type / default | Meaning |
| --- | --- | --- |
| `entry_points` | `bool \| Iterable = False` | `True` discovers the `noeta.plugins` group via `importlib.metadata`; an iterable of entry-point-like objects (`.name` + `.load()`) injects them (the testing seam); `False` discovers none |
| `modules` | `Sequence[str] = ()` | dotted module paths (`importlib.import_module`) or `.py` file paths (loaded by location); each must export `noeta_plugin` |
| `trusted_dirs` | `Sequence = ()` | directories scanned **unconditionally** for top-level `*.py` (files starting with `_` skipped) |
| `workspace_dirs` | `Sequence = ()` | directories scanned **only** when recorded in the trust store; otherwise skipped with `UntrustedPluginDirWarning` |
| `enabled` | `Iterable[str] \| None = None` | an allow-list of plugin names; when set, every other candidate is skipped before it is imported |
| `config` | `Mapping[str, dict] \| None = None` | plugin name → config dict, passed as the factory's second argument, **only** to a factory that declares a second positional parameter |
| `trust_store` | `Path \| None = None` | the trust store consulted for `workspace_dirs`; defaults to `DEFAULT_TRUST_STORE` (`~/.noeta/trust.json`) |
| `entry_point_group` | `str = "noeta.plugins"` | the entry-point group to discover |

Returns a `list[LoadedPlugin]` in discovery order (entry points, then modules,
then trusted dirs, then workspace dirs). Ordering here does **not** affect the
compiled spec — `merge_plugins` re-sorts.

### The three sources

1. **Entry points** — packaged, installed plugins. `entry_points=True` reads the
   `noeta.plugins` group; each entry point's loaded object is the plugin's
   `noeta_plugin` factory. This is the server-style source, paired with `enabled`.
2. **Explicit modules / files** — `modules` holds dotted import paths or `.py`
   file paths. The in-repo way to load a plugin without installing it.
3. **Directories** — `trusted_dirs` (unconditional) and `workspace_dirs`
   (trust-gated). Both scan top-level `*.py`, skipping files starting with `_`;
   each file must export `noeta_plugin`.

### Name derivation and the allow-list

A plugin's name is the entry-point name, or — for module / file / directory
sources — a module-level `noeta_plugin_name` override, falling back to the
module/file stem. `enabled` and `config` are keyed by that name.

`enabled` is always applied **before** the plugin is imported, so unapproved
code never runs. For file and directory sources the override is read
*statically* (an `ast` parse, no execution), which is why it must be a
module-level string **literal**:

```python
noeta_plugin_name = "block-shell"     # seen by the allow-list
noeta_plugin_name = _compute_name()   # honored for config/collisions, but the
                                      # allow-list falls back to the file stem
```

### Loud failure

A broken plugin fails loudly with `PluginError` naming the plugin — an import
error, a missing `noeta_plugin`, a non-callable factory, a factory raise, or a
duplicate plugin name found in two sources. The single non-raising skip is an
untrusted `workspace_dirs` entry, which warns with `UntrustedPluginDirWarning`.
The rule is deliberate: a bad plugin must fail the client build at startup, never
a mid-session turn.

## `merge_plugins(options, plugins) -> Options`

Fold `plugins` into `options`, returning a new `Options`.

- **Deterministic ordering.** Contributions are sorted by
  `(plugin name, contribution name)` before merge, so the compiled `AgentSpec` is
  invariant under plugin load order. Base tools keep their given order first,
  then the sorted plugin tools; guards and observers append in sorted-plugin
  order after the base's.
- **Tool expansion.** With `allowed_tools=None` and no plugin tools, it stays
  `None` (byte-identical default). With plugin tools, a `None` base first expands
  to the full built-in set, so plugins **add** tools rather than silently
  replacing the built-ins.
- **Collision = error.** Any tool, agent, content kind, or MCP alias contributed
  by two plugins or already present on the base `options`, or a second provider,
  raises `PluginError` naming **both** sources. There is no override flag in v1.
  The base's tool names include the tools its in-process `mcp_servers`
  contribute — `compile_options` flattens those onto the allow-list, so without
  the check one of the two would vanish silently.
- **Disallowed = error.** A contributed tool whose name is in the base's
  `disallowed_tools` raises too: compilation would drop it without a word,
  leaving an enabled plugin whose tool never exists.
- **Identity plane only** lands on the returned `Options`. Host-plane
  contributions (MCP specs, skill dirs) are collision-checked here but read via
  the accessors below — they have no `Options` surface.

The collision source for a name already on the base is labelled `<base options>`
in the error message; a plugin source is labelled `plugin '<name>'`.

## Host-plane accessors

Host-plane contributions never enter `Options`; a host collects them separately
and wires them into its `HostConfig`.

### `merged_mcp_servers(plugins) -> dict[str, spec]`

Collect the MCP server specs as `alias → spec`, sorted by plugin name. Raises
`PluginError` on an alias collision across plugins (the same check
`merge_plugins` performs). Wire the result into
`HostConfig.mcp_server_resolver`.

### `merged_skill_dirs(plugins) -> tuple[Path, ...]`

Collect the skill directories, de-duplicated and ordered by
`(plugin name, path)`; a directory contributed by more than one plugin appears
once. Wire the result into the host's skills directories.

## Trust store

The workspace-directory trust store is a JSON file — `{"trusted": [abs path, ...]}` —
at `DEFAULT_TRUST_STORE` (`~/.noeta/trust.json`) by default. Only `workspace_dirs`
consult it; `trusted_dirs` are always scanned.

| Function | Signature | Behaviour |
| --- | --- | --- |
| `is_trusted(path, store=None)` | `→ bool` | whether `path`'s canonical form is recorded; a missing store ⇒ `False`, never an error |
| `grant_trust(path, store=None)` | `→ None` | record `path`'s canonical form (idempotent); creates the store and its parent directory if absent |

Both sides canonicalise the path the same way — `~` expanded, absolute, symlinks
resolved — so how a path is spelled never decides trust: a grant written as
`~/ws/../ws/plugins` matches a lookup of `/home/me/ws/plugins`. Entries written
by hand are canonicalised on read too.

`store` defaults to `DEFAULT_TRUST_STORE` for both. A malformed (non-JSON) store
raises `PluginError` on read.

## Types and constants

| Type | Shape | Source |
| --- | --- | --- |
| `LoadedPlugin` | frozen: `name: str`, `contributions: PluginContributions` | `plugins.py` |
| `PluginContributions` | frozen: `tools`, `guards`, `observers`, `provider`, `content_kinds`, `agents`, `mcp_servers`, `skill_dirs` — one plugin factory's output; preserves the plugin's contribution order | `plugins.py` |
| `PluginError` | `RuntimeError` subclass — every load fault and merge collision | `plugins.py` |
| `UntrustedPluginDirWarning` | `UserWarning` — an untrusted `workspace_dirs` entry was skipped | `plugins.py` |

Module-level constants on `noeta.client.plugins` (not re-exported through
`noeta.sdk`):

- `PLUGIN_ENTRY_POINT_GROUP = "noeta.plugins"` — the SDK-owned runtime-plane
  entry-point group.
- `DEFAULT_TRUST_STORE = Path.home() / ".noeta" / "trust.json"` — the default
  trust store.

## See also

- [Write a plugin](../how-to/write-a-plugin.md) — the task-oriented guide
- [SDK reference](sdk.md) — the `Options` fields a plugin folds into
- [ADR: Plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md)
  — the design rationale (planes, deterministic merge, strict collisions)
