# `protected-paths` — a write fence around the agent's workspace

An example manifest plugin that contributes one `Guard` on the **`guard`**
surface. It inspects every file-mutating built-in tool call and **denies** it
when the target path escapes a configured allowlist of roots, or matches an
optional deny-glob.

The reason to want it: an agent with a `write` tool can address any path on the
machine, and "the prompt says stay in the workspace" is not an enforcement
mechanism. This is the packaged form of an ad-hoc `can_use_tool` path check — a
host enables it *by name* instead of writing guard code in every embedding.

`guard` is a **process-scoped** surface: loading the plugin puts the fence in
force for every agent in the process. There is no per-agent activation to
forget, which is the point — a write fence an operator can accidentally omit is
not a fence.

## What it inspects

| Tool | Path argument(s) read |
| --- | --- |
| `edit` | `arguments["path"]` |
| `write` | `arguments["path"]` |
| `apply_patch` | `arguments["edits"][*]["path"]` (every edit in the batch) |

Everything else passes untouched: read-only tools (`read` / `glob` / `grep`),
custom tools, and non-tool actions (spawn / finish). This fence is about where
writes land, nothing else.

`shell_run` is **out of scope on purpose**. A shell can reach anything on the
filesystem, so a path fence around it would be theatre. Confine shell IO with a
sandbox execution environment instead.

Malformed path arguments are skipped rather than denied — schema validation is
the tool's job, and a guard that ruled on arguments it cannot read would start
denying on tool-schema changes it has no opinion about.

## Containment is lexical — read this before you trust it

Each candidate path is normalized with `os.path.normpath` (collapsing `..`) and,
when relative, joined onto each allowed root before a component-wise containment
test (`noeta.sdk.path_within`). That catches the two classic escapes:

- `../../etc/passwd` — the `..` run collapses to a path outside every root.
- `/etc/passwd` — absolute, normalizes to itself, contained by no root.

**It does not resolve symlinks.** `normpath` is purely textual and never touches
the filesystem, so a symlink living *inside* an allowed root but pointing
*outside* it will pass. That cuts both ways and is a deliberate trade-off: a
textual check cannot be raced and cannot block, but it makes this a guardrail
against accidental and obvious escapes — **not a security sandbox**.

For symlink-safe fencing use the runtime's realpath-based `WorkspaceRoot`. For
real isolation use a sandbox execution environment.

Deny-globs err broad by design: a glob is matched against the raw path, its
normalized absolute form, **and** its basename, so `*.pem` fences the file
however the model chose to spell the path. A deny always beats containment — a
matching glob denies even inside an allowed root.

## Configuration

The manifest mechanism resolves a contribution's `ref` to a live object and
threads no per-plugin config dict; otherwise operator configuration would leak
into agent identity. Configuration therefore arrives through the environment,
read when the plugin module is imported:

| Environment variable | Meaning |
| --- | --- |
| `NOETA_PROTECTED_PATHS_ROOTS` | `os.pathsep`-separated writable roots. Unset ⇒ the process working directory — never empty, because a guard that protects nothing is worse than no guard. |
| `NOETA_PROTECTED_PATHS_DENY_GLOBS` | comma-separated [`fnmatch`](https://docs.python.org/3/library/fnmatch.html) patterns, matched against the raw path, its absolute form, or its basename. |

Set them **before** `load_plugins` runs — the shipped guard is built at import.
The [reference host](../../reference-host/host.py) sets
`NOETA_PROTECTED_PATHS_ROOTS` to the session workspace. `ProtectedPathsGuard` is
also directly constructable (`ProtectedPathsGuard(allowed_roots=…, deny_globs=…)`)
for tests and hosts that wire it themselves.

Configuring globs but no roots gives deny-glob-only mode: containment is off and
only the globs apply.

## Loading it

### From an explicit file path (local / dev, no install)

```python
import os
from pathlib import Path
from noeta.sdk import Client, load_plugins, presets

here = Path("examples/plugins/protected-paths")
os.environ["NOETA_PROTECTED_PATHS_ROOTS"] = str(here / "workspace")
pset = load_plugins(builtins=False, modules=[str(here / "plugin.py")])
client = Client(presets.main_options(), plugins=pset)  # guard is process-wide
```

### As an installed package

[`pyproject.toml`](./pyproject.toml) declares the `[tool.noeta]` manifest,
mirrored into [`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package data,
plus an entry point in the `noeta.plugins` group. The loader reads the manifest
**without importing any plugin code**, so listing and collision detection cost
nothing:

```python
import os
from noeta.sdk import Client, load_plugins, presets

os.environ["NOETA_PROTECTED_PATHS_ROOTS"] = "/srv/workspace"
pset = load_plugins(entry_points=True, enabled=["protected-paths"])
client = Client(presets.main_options(), plugins=pset)
```

The plugin's identity is the builder name — `PluginBuilder("protected-paths")` —
not the filename. That name is the enable-list key.

## Files

- [`plugin.py`](./plugin.py) — `ProtectedPathsGuard`, the env-configured `GUARD`,
  and the single-file `PluginBuilder` manifest.
- [`noeta-plugin.toml`](./noeta-plugin.toml) — the shipped static manifest.
- [`pyproject.toml`](./pyproject.toml) — the packaging convention (not built here).

`plugin.py`'s builder is the source of truth; keep the shipped manifest in
agreement with:

```bash
python -m noeta.sdk.plugin_check examples/plugins/protected-paths
```
