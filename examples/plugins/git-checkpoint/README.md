# `git-checkpoint` — workspace snapshots around mutating tool calls

A first-party example [Noeta plugin](../../../docs/reference/plugins.md). It
contributes one **Observer** that snapshots the workspace every time the agent
starts a mutating file tool call (`write` / `edit` / `apply_patch`), plus a
`restore_checkpoint` helper to roll the working tree back.

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
| `Options.observers` (wiring-layer) | `GitCheckpointObserver` — a `Callable[[EventEnvelope], None]` the host subscribes to the EventLog. |

Because the Observer is wiring-layer, enabling it **does not** change the
compiled `AgentSpec` identity or the cache prefix.

## Guard-observer contract

Per [`docs/adr/guard-observer-hooks.md`](../../../docs/adr/guard-observer-hooks.md),
an Observer failure must never flow back to the writer. The observer swallows
every exception (logging at `warning`): a missing or broken git repo degrades
to "no checkpoint recorded", never a failed agent turn. `restore_checkpoint`,
being an explicit operator call, does raise `GitCheckpointError` on failure.

## Configuration

The factory reads an operator `config` dict (all keys optional):

| Key | Default | Meaning |
| --- | --- | --- |
| `repo_path` | current working directory | the workspace git repo to checkpoint |
| `ref` | `refs/noeta/checkpoints` | the ref checkpoints are recorded on |
| `mutating_tools` | `("write", "edit", "apply_patch")` | tool names that trigger a checkpoint |

## Loading it

In this repository the plugin is loaded by **explicit path** (it is an example,
not an installed distribution):

```python
from noeta.client.plugins import load_plugins, merge_plugins

plugins = load_plugins(
    modules=["examples/plugins/git-checkpoint/plugin.py"],
    config={"git-checkpoint": {"repo_path": "/path/to/workspace"}},
)
options = merge_plugins(base_options, plugins)  # observer lands on Options
```

A real distribution declares the factory as a `noeta.plugins` entry point
instead (see [`pyproject.toml`](./pyproject.toml)); the loader then discovers
it via `load_plugins(entry_points=True)`.

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
