# `redaction` — a secret-scrubbing `tool_result_transform`

A first-party example [manifest plugin](../../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
demonstrating the new **`tool_result_transform`** surface (spec D9): a pure
`ToolResult -> ToolResult` stage applied **inside the ToolRuntime boundary,
before recording**. The transformed result *is* what gets recorded, so a
redaction stage means the secret never reaches the EventLog or the ContentStore
(acceptance criterion 10). It is a ToolRuntime pipeline stage, **not** a third
hook role — Guard/Observer stays exactly two roles.

## Activation (per-agent, unlike guards/observers)

`tool_result_transform` is a **per-agent activation** surface (spec D6): only an
agent that activates this plugin gets the stage.

```python
import dataclasses
from noeta.sdk import Client, load_plugin_set, presets

pset = load_plugin_set(builtins=False, modules=["examples/plugins/redaction/plugin.py"])
base = presets.main_options()
options = dataclasses.replace(base, plugins=tuple(base.plugins) + ("redaction",))
client = Client(options, plugins=pset)   # the main agent now scrubs tool results
```

Stages run in `(priority, plugin, name)` order; this one is `priority=50` so it
runs early, before any later stage could see the secret. The
[reference host](../../reference-host/host.py) activates this plugin, and
[`tests/test_reference_host.py`](../../../tests/test_reference_host.py) drives a
leaky tool through it end-to-end and asserts the durable ledger is secret-free.

## What it scrubs

Provider API keys (`sk-…` / `AKIA…`), bearer tokens, and `key=value` secrets are
replaced with `***REDACTED***` in the result's `summary` and anywhere a string
appears in its structured `output`. The transform is **pure and deterministic**
(the contract every transform owes, so replay and the stable-prefix cache are
undisturbed). Tune `_SECRET_PATTERNS` in [`plugin.py`](./plugin.py) to your own
secret formats.

## Verify the shipped manifest

```bash
python -m noeta.sdk.plugin_check examples/plugins/redaction
```
