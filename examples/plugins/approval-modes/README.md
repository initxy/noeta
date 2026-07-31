# `approval-modes` — one operator switch over every tool call

An example manifest plugin that contributes a single `Guard` on the **`guard`**
surface. It answers a question every host eventually has to answer — *how much
does this agent get to do without a human in the loop?* — with one switch
instead of bespoke `can_use_tool` code in every embedding.

`guard` is a process-scoped surface: loading the plugin puts the gate in force
for **every** agent in the process. There is no per-agent activation to forget,
which is the point — an approval fence an operator can accidentally omit is not
a fence.

## Modes

| Mode            | Verdict for a proposed tool call                                   |
| --------------- | ------------------------------------------------------------------ |
| `chat`          | **deny** every tool call (the agent reasons and answers, runs nothing) |
| `approve`       | **require approval** for every tool call — *default*                |
| `smart_approve` | **allow** low-risk tools; **require approval** for everything else  |
| `auto`          | **allow** every tool call                                          |

`smart_approve` classifies by **tool name**, not by a tool's declared
`risk_level`, so the guard needs no tool registry and stays self-contained. The
default low-risk set is only the read-only `read` / `grep` / `glob` / `ls`: a
tool missing from the set asks, so a classification gap fails towards the human.
Replace the set wholesale with the `low_risk_tools` key.

## Overrides

A per-tool override wins over the mode outright:

| Token    | Verdict            |
| -------- | ------------------ |
| `always` | allow              |
| `ask`    | require approval   |
| `never`  | deny               |

So `mode: auto` with `overrides: {write: never}` runs everything except `write`;
`mode: chat` with `overrides: {read: always}` runs nothing except `read`.

Only tool calls are gated. Subtask spawns and finishes pass through — approval
modes are about tool execution, and blocking a finish would strand a turn rather
than protect anything.

## Configuration

The manifest mechanism resolves a contribution's `ref` to a live object and
threads no per-plugin config dict, which keeps operator configuration out of
agent identity. Configuration therefore arrives through the environment, read
when the plugin module is imported:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `NOETA_APPROVAL_MODE` | `approve` | one of `chat` / `approve` / `smart_approve` / `auto` |

Set it **before** `load_plugins` runs — the shipped guard is built at import.

The finer knobs stay available to a host that constructs its own guard:

```python
guard = ApprovalModesGuard(build_policy({
    "mode": "smart_approve",
    "overrides": {"write": "never", "read": "always"},
    "low_risk_tools": ["read", "grep", "glob", "ls"],
}))
```

`build_policy` raises `ValueError` on an unknown mode, a bad override token, or
a non-list `low_risk_tools`, so a typo fails the client build instead of quietly
widening the fence mid-session.

## Loading it

In this repository the plugin is not installed, so load it by **explicit path**:

```python
import os
from noeta.sdk import Client, load_plugins, presets

os.environ["NOETA_APPROVAL_MODE"] = "smart_approve"
pset = load_plugins(builtins=False, modules=["examples/plugins/approval-modes/plugin.py"])
client = Client(presets.main_options(), plugins=pset)  # guard is process-wide
```

A real distribution ships the `[tool.noeta]` manifest mirrored into
[`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package data, plus an entry
point in the `noeta.plugins` group (see [`pyproject.toml`](./pyproject.toml));
a host then discovers it with `load_plugins(entry_points=True)`.

The plugin's identity is the builder name — `PluginBuilder("approval-modes")` —
not the filename. That name is the enable-list key.

## Files

- [`plugin.py`](./plugin.py) — `ApprovalPolicy`, `ApprovalModesGuard`, the
  env-configured `GUARD`, and the single-file `PluginBuilder` manifest.
- [`noeta-plugin.toml`](./noeta-plugin.toml) — the shipped static manifest the
  loader reads without importing plugin code.
- [`pyproject.toml`](./pyproject.toml) — the packaging convention (not built here).

Keep the two manifests in agreement with:

```bash
python -m noeta.sdk.plugin_check examples/plugins/approval-modes
```
