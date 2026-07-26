# `approval-modes` — goose-style tool-approval modes

A first-party example [Plugin](../../../docs/adr/plugin-contribution-bundles.md)
that contributes a single [`Guard`](../../../packages/noeta-runtime/noeta/protocols/hooks.py)
gating tool calls by an operator-chosen **mode**, with per-tool overrides. It is
the reference for a *config-driven* plugin: `noeta_plugin(api, config)` reads the
operator's config and folds it into one immutable policy.

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

## Config

All keys are optional:

```json
{
  "mode": "smart_approve",
  "overrides": { "write": "never", "read": "always" },
  "low_risk_tools": ["read", "grep", "glob", "ls"]
}
```

An unknown `mode`, a bad override token, or a non-list `low_risk_tools` raises at
factory time; the loader surfaces it as a `PluginError` naming this plugin, so a
misconfiguration fails the client build loudly rather than a mid-session turn.

## Loading

Installed as a package it is discovered through its `noeta.plugins` entry point
(see [`pyproject.toml`](./pyproject.toml)) with
`load_plugins(entry_points=True)`. In this repository it is loaded by **explicit
path**, no install:

```python
from noeta.sdk import Options, load_plugins, merge_plugins

plugins = load_plugins(
    modules=["examples/plugins/approval-modes/plugin.py"],
    config={"approval-modes": {"mode": "smart_approve", "overrides": {"write": "never"}}},
)
options = merge_plugins(Options(system_prompt="You are a helpful agent."), plugins)
```

The contributed guard lands on `options.guards`; the config is keyed by the
plugin's name (`approval-modes`), which the module fixes via
`noeta_plugin_name`.

Tested in [`tests/test_example_approval_modes.py`](../../../tests/test_example_approval_modes.py).
