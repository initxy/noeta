# `redaction` — a secret-scrubbing `tool_result_transform`

An example manifest plugin that contributes one stage on the
**`tool_result_transform`** surface: a pure `ToolResult -> ToolResult` transform
applied inside the ToolRuntime boundary, *before* the result is recorded. The
transformed result is what gets written, so a redaction stage means a leaked
secret never reaches the EventLog or the ContentStore.

The reason to want it: a tool can return a credential in its output or summary —
from an error message, a config dump, an echoed request — and once that lands in
the durable ledger it is there for every replay and every reader. Scrubbing at
the recording boundary is the one place that stops it at the source. This is a
pipeline stage, not a hook: governance has exactly two roles (Guard and
Observer), and a result transform is neither.

## What it scrubs

Provider API keys (`sk-…` / `AKIA…`), bearer tokens, and `key=value` secrets are
replaced with `***REDACTED***` in the result's `summary` and anywhere a string
appears in its structured `output`. The transform is **pure and deterministic**
— the contract every transform owes, so replay and the stable-prefix cache are
undisturbed. Tune `_SECRET_PATTERNS` in [`plugin.py`](./plugin.py) to your own
secret formats.

## Activation (per-agent, unlike guards/observers)

`tool_result_transform` is a **per-agent activation** surface: loading the plugin
is not enough, an agent must also list it in `Options.plugins`. Only an agent
that activates it gets the stage.

```python
import dataclasses
from noeta.sdk import Client, load_plugins, presets

pset = load_plugins(builtins=False, modules=["examples/plugins/redaction/plugin.py"])
base = presets.main_options()
options = dataclasses.replace(base, plugins=tuple(base.plugins) + ("redaction",))
client = Client(options, plugins=pset)   # the main agent now scrubs tool results
```

Stages run in `(priority, plugin, name)` order; this one is `priority=50` so it
runs early, before any later stage could see the secret. The
[reference host](../../reference-host/host.py) activates this plugin, and
[`tests/test_reference_host.py`](../../../tests/test_reference_host.py) drives a
leaky tool through it end-to-end and asserts the durable ledger is secret-free.

A real distribution ships the `[tool.noeta]` manifest mirrored into
[`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package data, plus an entry
point in the `noeta.plugins` group (see [`pyproject.toml`](./pyproject.toml)); a
host then discovers it with `load_plugins(entry_points=True)`.

## Verify the shipped manifest

```bash
python -m noeta.sdk.plugin_check examples/plugins/redaction
```
