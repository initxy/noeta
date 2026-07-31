# `git-checkpoint` — workspace snapshots around mutating tool calls

A first-party example [manifest plugin](../../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md).
It contributes one **Observer** on the `observer` surface that snapshots the
workspace every time the agent starts a mutating file tool call (`write` /
`edit` / `apply_patch`), plus a `restore_checkpoint` helper to roll the working
tree back.

Checkpoints are recorded on a dedicated ref (`refs/noeta/checkpoints`) through
a **temporary git index**, so they never touch the user's branch, `HEAD`, or
staging area:

- The ref lives outside `refs/heads/*`, so it is not a branch, does not move
  `HEAD`, and never shows up in the user's `git log`.
- Snapshots are built with `GIT_INDEX_FILE` pointing at a scratch index, so
  the real `.git/index` is neither read nor written.
- Checkpoints chain (each parent is the previous checkpoint), so the ref holds
  the full ordered snapshot history.

## What it contributes

| Surface | Contribution |
| --- | --- |
| `observer` (governance, process-wide — spec D6) | `GitCheckpointObserver` — a `Callable[[EventEnvelope], None]` the Client subscribes to the EventLog. |

`observer` is a wiring-layer governance surface: enabling it **does not** change
the compiled `AgentSpec` identity or the cache prefix, and — like `guard` — a
loaded observer is in force for every agent in the process (never gated on
per-agent activation).

## Guard-observer contract

Per [`docs/adr/guard-observer-hooks.md`](../../../docs/adr/guard-observer-hooks.md),
an Observer failure must never flow back to the writer. The observer swallows
every exception (logging at `warning`): a missing or broken git repo degrades
to "no checkpoint recorded", never a failed agent turn. `restore_checkpoint`,
being an explicit operator call, does raise `GitCheckpointError` on failure.

## Configuration — via the environment

The manifest mechanism resolves a `ref` to a live object and does not thread a
per-plugin config dict; configuration is read from the environment when the
plugin module is imported:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `NOETA_GIT_CHECKPOINT_REPO` | current working directory | the workspace git repo to checkpoint |
| `NOETA_GIT_CHECKPOINT_REF` | `refs/noeta/checkpoints` | the ref checkpoints are recorded on |

The mutating-tool set is a construction knob of `GitCheckpointObserver`
(`mutating_tools=…`); the shipped manifest uses the default
`("write", "edit", "apply_patch")`.

## Loading it

In this repository the plugin is loaded by **explicit path** (it is an example,
not an installed distribution):

```python
import os
from noeta.sdk import Client, load_plugins, presets

os.environ["NOETA_GIT_CHECKPOINT_REPO"] = "/path/to/workspace"
pset = load_plugins(builtins=False, modules=["examples/plugins/git-checkpoint/plugin.py"])
client = Client(presets.main_options(), plugins=pset)  # observer subscribed process-wide
```

A real distribution ships the `[tool.noeta]` manifest + a `noeta.plugins` entry
point (see [`pyproject.toml`](./pyproject.toml)); the loader then discovers it
via `load_plugins(entry_points=True)`. Verify the shipped manifest with
`python -m noeta.sdk.plugin_check examples/plugins/git-checkpoint`.

## Restoring

```python
from importlib import import_module  # or load the module by path

restore_checkpoint("/path/to/workspace")                 # restore the ref tip
restore_checkpoint("/path/to/workspace", commit="<sha>") # restore a specific checkpoint
```

Restore overwrites the files recorded in the checkpoint and leaves files
created after it in place (a non-destructive restore); it never moves `HEAD`
or the user's index.

## Inspecting checkpoints

```bash
git log --oneline refs/noeta/checkpoints     # the snapshot chain
git diff refs/noeta/checkpoints -- .         # working tree vs the latest checkpoint
```
