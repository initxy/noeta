# `protected-paths` — a path-containment Guard, packaged as a manifest plugin

A first-party example [manifest plugin](../../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
that contributes **one `Guard`** on the `guard` surface. The guard inspects
every file-mutating built-in tool call and **denies** it when the target path
escapes a configured allowlist of roots, or matches an optional deny-glob.

`guard` is a **governance** surface (spec D6): once the plugin is loaded the
guard is in force for **every** agent in the process — an operator cannot opt an
agent out of a write fence by omitting an activation. It is the packaged form of
an ad-hoc `can_use_tool` path check: any host can enable it *by name* instead of
writing guard code.

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

## Configuration — via the environment

The manifest mechanism resolves a contribution's `ref` to a live object; it does
**not** thread a per-plugin config dict (that would make agent identity depend on
operator config). Configuration is therefore *orthogonal to identity*, read from
the environment when the plugin module is imported:

| Environment variable | Meaning |
| --- | --- |
| `NOETA_PROTECTED_PATHS_ROOTS` | `os.pathsep`-separated writable roots. Absent ⇒ the process working directory (so the guard always protects *something*). |
| `NOETA_PROTECTED_PATHS_DENY_GLOBS` | comma-separated [`fnmatch`](https://docs.python.org/3/library/fnmatch.html) patterns. A match on the raw path, its normalized absolute form, **or** its basename denies the call, *even inside an allowed root*. |

A host injects these before it loads the plugin — the
[reference host](../../reference-host/host.py) sets `NOETA_PROTECTED_PATHS_ROOTS`
to the session workspace. The `ProtectedPathsGuard` is also independently
constructable (`ProtectedPathsGuard(allowed_roots=…, deny_globs=…)`) for tests
and hosts that wire it directly.

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

### As an installed package (entry point + shipped manifest)

The [`pyproject.toml`](./pyproject.toml) declares a `[tool.noeta]` static
manifest (mirrored to [`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package
data) plus an entry point in the `noeta.plugins` group. The loader reads the
manifest **without importing any plugin code**:

```python
import os
from noeta.sdk import Client, load_plugins, presets

os.environ["NOETA_PROTECTED_PATHS_ROOTS"] = "/srv/workspace"
pset = load_plugins(entry_points=True, enabled=["protected-paths"])
client = Client(presets.main_options(), plugins=pset)  # guard is process-wide
```

### From an explicit file path (local / dev, no install)

```python
import os
from pathlib import Path
from noeta.sdk import Client, load_plugins, presets

here = Path(__file__).parent
os.environ["NOETA_PROTECTED_PATHS_ROOTS"] = str(here / "workspace")
pset = load_plugins(builtins=False, modules=[str(here / "plugin.py")])
client = Client(presets.main_options(), plugins=pset)
```

The single-file `plugin = PluginBuilder("protected-paths")` fixes the plugin's
identity name (the enable-list key and the collision label) even though the file
is `plugin.py`. The tests
([`tests/test_example_protected_paths.py`](../../../tests/test_example_protected_paths.py))
load it exactly this way.

## Verifying the shipped manifest

`plugin.py`'s decorators are the source of truth; the shipped `noeta-plugin.toml`
must match them. Verify with:

```bash
python -m noeta.sdk.plugin_check examples/plugins/protected-paths
```

## Files

- [`plugin.py`](./plugin.py) — the `ProtectedPathsGuard`, the env-configured
  `GUARD` instance, and the single-file `PluginBuilder` manifest.
- [`noeta-plugin.toml`](./noeta-plugin.toml) — the shipped static manifest.
- [`pyproject.toml`](./pyproject.toml) — the `[tool.noeta]` + entry-point
  packaging convention (not built or tested here).
