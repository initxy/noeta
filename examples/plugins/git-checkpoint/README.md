# `git-checkpoint` — undo for the agent's file edits

An example manifest plugin that contributes one `Observer` on the **`observer`**
surface. It snapshots the workspace every time the agent *starts* a mutating file
tool call (`write` / `edit` / `apply_patch`), and ships `restore_checkpoint` to
roll the working tree back to any of those points.

The reason to want it: an agent editing files is one bad turn away from work you
cannot get back, and asking the user to commit before every session is not a
plan. Snapshotting on tool *start* — not completion — is what makes each
checkpoint a usable undo point: it captures the tree the call is about to change.

## It never touches your git state

Checkpoints land on `refs/noeta/checkpoints`, built through a scratch index:

- The ref lives outside `refs/heads/*`, so it is not a branch, does not move
  `HEAD`, and never appears in your `git log`.
- Snapshots are staged with `GIT_INDEX_FILE` pointing at a throwaway index, so
  the real `.git/index` is neither read nor written — whatever you had staged
  stays staged.
- Each checkpoint's parent is the previous one, so a plain ref carries the full
  ordered history without ever being a branch.
- Commits carry an explicit `noeta-checkpoint` identity, so they work in a repo
  with no `user.name` configured and can never be mistaken for yours.

## What it contributes

| Surface | Contribution |
| --- | --- |
| `observer` | `GitCheckpointObserver` — a `Callable[[EventEnvelope], None]` the Client subscribes to the EventLog. |

`observer` is a **process-scoped** surface: loading the plugin arms
checkpointing for every agent in the process, with no per-agent activation to
forget. Observers are wiring, never identity — enabling one leaves the compiled
`AgentSpec` and the cache prefix untouched.

## Failure is not the agent's problem

Per [`docs/adr/guard-observer-hooks.md`](../../../docs/adr/guard-observer-hooks.md)
an observer failure must never flow back to the writer, so the observer swallows
every exception and logs at `warning`. A missing or broken git repo degrades to
"no checkpoint recorded" rather than a failed turn: losing an undo point is
recoverable, losing the turn is not.

`restore_checkpoint` inverts that — it is an explicit operator call, so it raises
`GitCheckpointError` rather than silently doing nothing.

## Configuration

The manifest mechanism resolves a contribution's `ref` to a live object and
threads no per-plugin config dict, which keeps operator configuration out of
agent identity. Configuration therefore arrives through the environment, read
when the plugin module is imported:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `NOETA_GIT_CHECKPOINT_REPO` | current working directory | the workspace git repo to checkpoint |
| `NOETA_GIT_CHECKPOINT_REF` | `refs/noeta/checkpoints` | the ref checkpoints are recorded on |

Set them **before** `load_plugins` runs — the shipped observer is built at
import, and the `cwd` default is rarely the workspace a real host means.

The mutating-tool set is a construction knob of `GitCheckpointObserver`
(`mutating_tools=…`); the shipped manifest takes the default
`("write", "edit", "apply_patch")`.

## Loading it

In this repository the plugin is not installed, so load it by **explicit path**:

```python
import os
from noeta.sdk import Client, load_plugins, presets

os.environ["NOETA_GIT_CHECKPOINT_REPO"] = "/path/to/workspace"
pset = load_plugins(builtins=False, modules=["examples/plugins/git-checkpoint/plugin.py"])
client = Client(presets.main_options(), plugins=pset)  # observer subscribed process-wide
```

A real distribution ships the `[tool.noeta]` manifest mirrored into
[`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package data, plus an entry
point in the `noeta.plugins` group (see [`pyproject.toml`](./pyproject.toml));
a host then discovers it with `load_plugins(entry_points=True)`.

## Restoring

```python
restore_checkpoint("/path/to/workspace")                 # the ref tip
restore_checkpoint("/path/to/workspace", commit="<sha>") # a specific checkpoint
```

Restore overwrites the files recorded in the checkpoint and leaves files created
after it in place — an undo that also deleted unrelated new work would be worse
than the mistake it reverses. It never moves `HEAD` or your index.

## Inspecting checkpoints

```bash
git log --oneline refs/noeta/checkpoints     # the snapshot chain
git diff refs/noeta/checkpoints -- .         # working tree vs the latest checkpoint
```

## Files

- [`plugin.py`](./plugin.py) — `GitCheckpointObserver`, `restore_checkpoint`,
  the env-configured `OBSERVER`, and the single-file `PluginBuilder` manifest.
- [`noeta-plugin.toml`](./noeta-plugin.toml) — the shipped static manifest the
  loader reads without importing plugin code.
- [`pyproject.toml`](./pyproject.toml) — the packaging convention (not built here).

Keep the two manifests in agreement with:

```bash
python -m noeta.sdk.plugin_check examples/plugins/git-checkpoint
```
