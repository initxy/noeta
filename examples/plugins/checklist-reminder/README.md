# `checklist-reminder` — nudge the agent when its checklist gets long

An example manifest plugin that contributes one render on the **`reminder`**
surface. It exists to show the cheapest way to steer an agent's behaviour: a few
lines of pure Python that append a sentence to the composed request when — and
only when — a condition in the folded state holds.

Here the condition is a checklist with more than five unfinished items, and the
sentence suggests delegating the independent ones to sub-agents instead of
grinding the whole list in one long context.

## What a reminder is

`(name, priority, render)`, where `render` maps a narrow projection of folded
state to `str | None`. A non-`None` string is wrapped in one
`<system-reminder>` message at the **tail of the dynamic suffix**, so the stable
prefix is untouched by construction.

Purity is the contract — no clock, no randomness, no external fetch. The same
folded state must compose the same bytes, or replay diverges and the cached
stable prefix stops holding. A reminder that genuinely needs the outside world
belongs on `reminder_provider`, where the output is recorded once and folded
back from the ledger on resume.

Returning `None` renders nothing, which is what keeps this reminder
self-limiting: a short or finished list is silent, so the nudge cannot decay
into noise the model learns to skip.

`priority=400` places it after the three built-in reminders (`unfinished-todos`
100, `delegation-nudge` 200, `read-suggestion` 300) — an advisory note should
not displace the ones the agent acts on.

The render reads the projection by duck typing (`view.todos` only), which keeps
this example dependent on nothing outside `noeta.sdk`.

## Loading and activating it

`reminder` is a **per-agent** surface: loading the plugin is not enough, an agent
must also activate it by name. An agent that does not activate the plugin
renders none of its reminders.

```python
import dataclasses
from noeta.sdk import Client, load_plugins, presets

pset = load_plugins(builtins=False, modules=["examples/plugins/checklist-reminder/plugin.py"])
base = presets.main_options()
options = dataclasses.replace(base, plugins=tuple(base.plugins) + ("checklist-reminder",))
client = Client(options, plugins=pset)
```

The activation key is the builder name — `PluginBuilder("checklist-reminder")` —
not the filename. The [reference host](../../reference-host/README.md) activates
its plugins the same way.

A real distribution ships the `[tool.noeta]` manifest mirrored into
[`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package data, plus an entry
point in the `noeta.plugins` group (see [`pyproject.toml`](./pyproject.toml));
a host then discovers it with `load_plugins(entry_points=True)`.

The built-in reminders ride this same surface — they are contributions of the
`reminders` built-in plugin, not a privileged path.

## Files

- [`plugin.py`](./plugin.py) — the render and the single-file `PluginBuilder`
  manifest.
- [`noeta-plugin.toml`](./noeta-plugin.toml) — the shipped static manifest the
  loader reads without importing plugin code.
- [`pyproject.toml`](./pyproject.toml) — the packaging convention (not built here).

Keep the two manifests in agreement with:

```bash
python -m noeta.sdk.plugin_check examples/plugins/checklist-reminder
```
