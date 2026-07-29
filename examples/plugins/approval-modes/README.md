# `approval-modes` — goose-style tool-approval modes

A first-party example [manifest plugin](../../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
that contributes a single `Guard` on the `guard` surface (governance,
process-wide — spec D6), gating tool calls by an operator-chosen **mode**, with
per-tool overrides. It is the reference for packaging a *configured* object: the
guard's immutable `ApprovalPolicy` is built from the environment at import.

## Modes

| Mode            | Verdict for a proposed tool call                                             |
| --------------- | ---------------------------------------------------------------------------- |
| `chat`          | **deny** every tool call (reason and answer only — no tools run)             |
| `approve`       | **require approval** for every tool call — *default*                          |
| `smart_approve` | **allow** low-risk tools; **require approval** for everything else            |
| `auto`          | **allow** every tool call                                                    |

`smart_approve` classifies risk by **tool name**, not by the tool's declared
`risk_level`, so the guard stays self-contained (it needs no tool registry). The
default low-risk set is conservative — only the read-only `read` / `grep` /
`glob` / `ls` — and is replaced wholesale with the `low_risk_tools` config key.

## Overrides

A per-tool override always wins over the mode:

| Token    | Verdict            |
| -------- | ------------------ |
| `always` | allow              |
| `ask`    | require approval   |
| `never`  | deny               |

So `mode: auto` with `overrides: {write: never}` runs everything except `write`;
`mode: chat` with `overrides: {read: always}` runs nothing except `read`.

The guard gates only tool calls — subtask spawns and finishes pass through
(approval is about *tool execution*, matching the built-in `PermissionGuard`).

## Config — via the environment

The shipped guard reads its mode from the environment when the plugin module is
imported:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `NOETA_APPROVAL_MODE` | `approve` | one of `chat` / `approve` / `smart_approve` / `auto` |

The finer knobs (`overrides`, `low_risk_tools`) remain available to a host that
constructs its own guard directly:

```python
from importlib import import_module  # or load plugin.py by path
mod = ...  # the plugin module
guard = mod.ApprovalModesGuard(mod.build_policy({
    "mode": "smart_approve",
    "overrides": {"write": "never", "read": "always"},
    "low_risk_tools": ["read", "grep", "glob", "ls"],
}))
```

`build_policy` raises `ValueError` on an unknown `mode`, a bad override token, or
a non-list `low_risk_tools`, so a misconfiguration fails loudly.

## Loading

Installed as a package it is discovered through its `[tool.noeta]` manifest +
`noeta.plugins` entry point (see [`pyproject.toml`](./pyproject.toml)) with
`load_plugin_set(entry_points=True)`. In this repository it is loaded by
**explicit path**, no install:

```python
import os
from noeta.sdk import Client, load_plugin_set, presets

os.environ["NOETA_APPROVAL_MODE"] = "smart_approve"
pset = load_plugin_set(builtins=False, modules=["examples/plugins/approval-modes/plugin.py"])
client = Client(presets.main_options(), plugins=pset)  # guard is process-wide
```

The single-file `plugin = PluginBuilder("approval-modes")` fixes the plugin's
identity name. Verify the shipped manifest with
`python -m noeta.sdk.plugin_check examples/plugins/approval-modes`.

Tested in [`tests/test_example_approval_modes.py`](../../../tests/test_example_approval_modes.py).
