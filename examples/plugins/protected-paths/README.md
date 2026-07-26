# `protected-paths` — a path-containment Guard, packaged as a plugin

A first-party example [plugin](../../../docs/adr/plugin-contribution-bundles.md)
that contributes **one `Guard`**. The guard inspects every file-mutating
built-in tool call and **denies** it when the target path escapes a configured
allowlist of roots, or matches an optional deny-glob.

It is the packaged, operator-configurable form of an ad-hoc `can_use_tool`
path check: any host can enable it *by name* instead of writing guard code.

## What it inspects

| Tool | Path argument(s) read |
| --- | --- |
| `edit` | `arguments["path"]` |
| `write` | `arguments["path"]` |
| `apply_patch` | `arguments["edits"][*]["path"]` (every edit in the batch) |

Everything else is allowed untouched: read-only tools (`read` / `glob` /
`grep`), custom tools, and non-tool actions (spawn / finish). `shell_run` is
**out of scope on purpose** — a shell can touch anything on the filesystem, so
a path guard cannot fence it. Confine shell IO with a sandbox execution
environment instead.

## Configuration

The plugin factory reads two keys from its per-plugin config dict, both
individually optional:

```jsonc
{
  "allowed_roots": ["/abs/workspace", "/abs/scratch"],
  "deny_globs": ["*.env", "*/secrets/*", "id_rsa"]
}
```

- **`allowed_roots`** — directories a write may land in. Relative entries are
  resolved against the process working directory. Empty ⇒ the containment
  check is disabled (the plugin runs in deny-glob-only mode).
- **`deny_globs`** — [`fnmatch`](https://docs.python.org/3/library/fnmatch.html)
  patterns. A match on the raw path, its normalized absolute form, **or** its
  basename denies the call, *even inside an allowed root*. Denies always win.

Enabling the plugin with **neither** key is a misconfiguration and fails the
client build loudly — a protection guard that protects nothing is a bug, not a
sensible default.

## Containment is lexical — read this before you trust it

The check normalizes each candidate path with `os.path.normpath` (collapsing
`..` segments) and, when the path is relative, joins it onto each allowed root
before a component-wise containment test
([`noeta.sdk.path_within`](../../../packages/noeta-sdk/noeta/sdk/__init__.py)).
That catches the two classic escapes:

- `../../etc/passwd` — the `..` run collapses to a path outside every root.
- `/etc/passwd` (absolute) — normalizes to itself, contained by no root.

**It does not resolve symlinks.** `normpath` is purely textual and never
touches the filesystem, so a symlink that lives *inside* an allowed root but
points *outside* it will pass this guard. Lexical containment is a guardrail
against accidental or obvious escapes — **not a security sandbox**. For
symlink-safe fencing use the runtime's realpath-based `WorkspaceRoot`, and for
true isolation use a sandbox execution environment. This trade-off is
deliberate.

## Enabling it

### As an installed package (entry point)

The [`pyproject.toml`](./pyproject.toml) here declares an entry point in the
`noeta.plugins` group the SDK owns. Once such a package is installed, discover
it and thread its config by name:

```python
from noeta.sdk import Options, load_plugins, merge_plugins

plugins = load_plugins(
    entry_points=True,
    enabled=["protected-paths"],  # server-style: explicit enable-list
    config={"protected-paths": {"allowed_roots": ["/srv/workspace"]}},
)
options = merge_plugins(Options(system_prompt="…"), plugins)
```

### From an explicit file path (local / dev, no install)

```python
from pathlib import Path
from noeta.sdk import Options, load_plugins, merge_plugins

here = Path(__file__).parent
plugins = load_plugins(
    modules=[str(here / "plugin.py")],
    config={"protected-paths": {"allowed_roots": [str(here / "workspace")]}},
)
options = merge_plugins(Options(system_prompt="…"), plugins)
```

The module sets `noeta_plugin_name = "protected-paths"`, so the plugin's name
(the enable-list key, the `config` key, and the collision label) stays
`protected-paths` even though the file is `plugin.py`. The tests
([`tests/test_example_protected_paths.py`](../../../tests/test_example_protected_paths.py))
load it exactly this way.

## Files

- [`plugin.py`](./plugin.py) — the `ProtectedPathsGuard` and the
  `noeta_plugin(api, config)` factory.
- [`pyproject.toml`](./pyproject.toml) — an illustration of the entry-point
  packaging convention (not built or tested here).
