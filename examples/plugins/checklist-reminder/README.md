# `checklist-reminder` — a compose-time reminder (track B)

A first-party example [manifest plugin](../../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
demonstrating the new **`reminder`** surface — **track B** (spec D8): a
compose-time, **pure** reminder. A `reminder` is `(name, priority, render)` where
`render` is a *pure function of a narrow folded-state projection* returning
`str | None`, rendered at the **tail of the dynamic suffix** (the composer wraps
a non-`None` string in one `<system-reminder>` message). The stable prefix is
untouched by construction.

Purity is the contract (the same trust class as a `ContentKindSpec` renderer): no
clock, no randomness, no external fetch — so the same folded state always
composes the same bytes, and replay / the KV-cache prefix stay reproducible.

## What it renders

When the agent's checklist grows past `THRESHOLD` unfinished items, it appends a
scope-hygiene nudge — a pure function of the projection's `todos`. A short or
finished list renders nothing (self-limiting, never nags). Its `priority=400`
places it **after** the three built-in reminders (`unfinished-todos` 100,
`delegation-nudge` 200, `read-suggestion` 300).

The `render` reads the composer's narrow `ReminderView` projection by duck typing
(only `view.todos`), so the example stays on the `noeta.sdk` public surface.

## Note on wiring

`reminder` is a **per-agent activation** surface (spec D6): the Client folds an
activated plugin's renders into that agent's composer reminder registry, and an
agent that did not activate the plugin renders none of them. Name your plugin in
`Options.plugins` (or an `AgentDefinition.plugins`) to switch it on — the
[reference host](../../reference-host/README.md) does exactly that.

Unit-tested against the runtime `ReminderRegistry` in
[`tests/test_example_new_surfaces.py`](../../../tests/test_example_new_surfaces.py);
the end-to-end path (activate → the text ships in the composed request) is pinned
by [`tests/test_plugin_wiring_contract.py`](../../../tests/test_plugin_wiring_contract.py).
The built-in reminders already ride this surface (the `reminders` built-in plugin).

## Verify the shipped manifest

```bash
python -m noeta.sdk.plugin_check examples/plugins/checklist-reminder
```
